import string
import re
from collections import defaultdict
from fnmatch import fnmatchcase
from typing import Any, Dict, List, Optional, Sequence

import click
import progressbar
import python_freeipa

from .status import Status, print_status
from .utils import ObjectManager
from .statistics import Stats


CREATION_TIME_RE = re.compile(r"([0-9 :-]+).[0-9]+\+00:00")


class Users(ObjectManager):
    def __init__(self, *args, agreements, **kwargs):
        super().__init__(*args, **kwargs)
        self.agreements = agreements

    @staticmethod
    def _make_user_patterns(
        users_start_at: Optional[str], restrict_users: Optional[Sequence[str]],
    ) -> List[str]:
        if restrict_users:
            user_patterns = [
                pattern
                for pattern in restrict_users
                if not users_start_at
                or pattern.replace("*", "\u0010ffff") >= users_start_at
            ]
        else:
            restrict_users = ()
            alphabet = list(string.ascii_lowercase)
            if users_start_at:
                start_index = alphabet.index(users_start_at[0].lower())
                user_patterns = [
                    pattern + "*"
                    for pattern in [users_start_at] + alphabet[start_index + 1 :]
                ]
            else:
                user_patterns = [pattern + "*" for pattern in alphabet]

        return user_patterns

    def pull_from_fas(
        self,
        users_start_at: Optional[str] = None,
        restrict_users: Optional[Sequence[str]] = None,
    ) -> Dict[str, List[Dict]]:
        user_patterns = self._make_user_patterns(users_start_at, restrict_users)

        fas_matched_users = {}

        for fas_name, fas_inst in self.fas_instances.items():
            matched_users = fas_matched_users[fas_name] = []

            for pattern in user_patterns:
                if "*" in pattern:
                    click.echo(f"[{fas_name}] finding users matching {pattern!r}")
                else:
                    click.echo(f"[{fas_name}] finding user {pattern!r}")

                result = fas_inst.send_request(
                    "/user/list",
                    req_params={"search": pattern},
                    auth=True,
                    timeout=240,
                )

                people = result["unapproved_people"] + result["people"]
                if users_start_at:
                    matched_users.extend(
                        u for u in people if u.username >= users_start_at
                    )
                else:
                    matched_users.extend(people)

        return fas_matched_users

    def push_to_ipa(
        self,
        users: List[Dict],
        users_start_at: Optional[str] = None,
        restrict_users: Optional[Sequence[str]] = None,
        conflicts: Optional[Dict[str, Sequence[Dict[str, Any]]]] = None,
    ) -> Stats:
        stats = Stats()

        users_stats = self._push_users(users, users_start_at, restrict_users, conflicts)
        stats.update(users_stats)

        return stats

    def _push_users(self, fas_users, users_start_at, restrict_users, conflicts):
        counter = 0
        added = 0
        edited = 0
        skipped = 0
        groups_to_member_usernames = defaultdict(list)
        groups_to_unapproved_member_usernames = defaultdict(list)
        groups_to_sponsor_usernames = defaultdict(list)
        agreements_to_usernames = defaultdict(list)

        user_patterns = self._make_user_patterns(users_start_at, restrict_users)
        if not conflicts:
            conflicts = {}
        skip_conflicts = set(self.config["users"].get("skip_conflicts", ()))

        for fas_name, users in fas_users.items():
            print(f"{fas_name}: {len(users)} found")
            if not users:
                continue

            fas_conf = self.config["fas"][fas_name]

            users.sort(key=lambda u: u["username"])

            max_length = max([len(u["username"]) for u in users])

            for person in progressbar.progressbar(users, redirect_stdout=True):
                username = person["username"]
                if all(not fnmatchcase(username, pat) for pat in user_patterns):
                    continue

                user_conflicts = set(conflicts.get(username, ()))
                user_skip_conflicts = skip_conflicts & user_conflicts
                if user_skip_conflicts:
                    print_status(
                        Status.FAILED,
                        f"[{fas_name}] Skipping user '{username}' because of conflicts:"
                        f" {', '.join(user_skip_conflicts)}",
                    )
                    skipped += 1
                    continue

                counter += 1
                self.check_reauth(counter)
                click.echo(username.ljust(max_length + 2), nl=False)
                # Add user
                status = self.migrate_user(person)
                if status != Status.SKIPPED:
                    # Record membership
                    for _groupname, membership in person["group_roles"].items():
                        if (
                            _groupname in fas_conf["groups"].get("ignore", ())
                            or membership["group_id"] is None  # empty list of groups
                        ):
                            continue
                        groupname = fas_conf["groups"].get("prefix", "") + _groupname
                        if membership["role_status"] == "approved":
                            groups_to_member_usernames[groupname].append(
                                person["username"]
                            )
                            if membership["role_type"] in ["administrator", "sponsor"]:
                                groups_to_sponsor_usernames[groupname].append(
                                    person["username"]
                                )
                        else:
                            groups_to_unapproved_member_usernames[groupname].append(
                                person["username"]
                            )
                    # Record agreement signatures
                    group_names = [g["name"] for g in person["memberships"]]
                    for agreement in fas_conf.get("agreement", ()):
                        if set(agreement["signed_groups"]) & set(group_names):
                            # intersection is not empty: the user signed it
                            agreements_to_usernames[agreement["name"]].append(
                                person["username"]
                            )

                # Status
                print_status(status)
                if status == Status.ADDED:
                    added += 1
                elif status == Status.UPDATED:
                    edited += 1
                elif status == Status.SKIPPED:
                    skipped += 1

        self.agreements.record_user_signatures(agreements_to_usernames)
        self.add_users_to_groups(groups_to_member_usernames, "members")
        self.add_users_to_groups(groups_to_sponsor_usernames, "sponsors")
        self.remove_users_from_groups(groups_to_unapproved_member_usernames)
        return {
            "user_counter": counter,
            "users_added": added,
            "users_edited": edited,
            "users_skipped": skipped,
        }

    @classmethod
    def _compact_dict(cls, val):
        # If it has ID fields, it's just to bulky and uninformative.
        if any("id" in key for key in val):
            return "{…}"

        items_strs = (f"'{k}': …" for k in val.keys())
        return f"{{{', '.join(items_strs)}}}"

    @classmethod
    def _compact_sequence(cls, val):
        return (cls._compact_value(item) for item in val)

    @classmethod
    def _compact_list(cls, val):
        return list(cls._compact_sequence(val))

    @classmethod
    def _compact_tuple(cls, val):
        return tuple(cls._compact_sequence(val))

    @classmethod
    def _compact_set(cls, val):
        return set(cls._compact_sequence(val))

    @classmethod
    def _compact_value(cls, val):
        if isinstance(val, dict):
            return cls._compact_dict(val)
        elif isinstance(val, list):
            return cls._compact_list(val)
        elif isinstance(val, tuple):
            return cls._compact_tuple(val)
        elif isinstance(val, set):
            return cls._compact_set(val)
        else:
            return val

    def migrate_user(self, person):
        person_orig = person
        person = person.copy()
        if self.config["users"]["skip_disabled"] and person.get("status") != "active":
            return Status.SKIPPED
        if (
            self.config["users"]["skip_spam"]
            and person.get("status") == "spamcheck_denied"
        ):
            return Status.SKIPPED
        if self.config["skip_user_add"]:
            return Status.UNMODIFIED

        # Don't modify the original object, and remove all key/value pairs that should
        # be ignored
        ignored_keys = {
            "affiliation",
            "alias_enabled",
            "certificate_serial",
            "comments",
            "country_code",
            "facsimile",
            "group_roles",
            "id",
            "internal_comments",
            "ipa_sync_status",
            "last_seen",
            "latitude",
            "longitude",
            "memberships",
            "old_password",
            "password",
            "password_changed",
            "postal_address",
            "roles",
            "security_answer",
            "security_question",
            "status_change",
            "telephone",
            "unverified_email",
        }
        ignored_key_substrings = ("token",)
        person = {
            key: value
            for key, value in person.items()
            if not (
                key in ignored_keys or any(t in key for t in ignored_key_substrings)
            )
        }

        # Pop all key/value pairs that are processed
        username = person.pop("username")
        human_name = person.pop("human_name")
        status = person.pop("status")
        email = person.pop("email")
        ircnick = person.pop("ircnick")
        locale = person.pop("locale")
        timezone = person.pop("timezone")
        gpg_keyid = person.pop("gpg_keyid")
        ssh_key = person.pop("ssh_key")
        creation = person.pop("creation")
        privacy = person.pop("privacy")

        # Fail if any details are left, i.e. unprocessed
        if person:
            print("Unprocessed details:")
            for key, value in sorted(person.items(), key=lambda x: x[0]):
                if (
                    key in {"email", "ssh_key", "telephone", "facsimile"}
                    or "password" in key
                ):
                    print(f"\t{key}: <…shhhhh…>")
                else:
                    print(f"\t{key}: {self._compact_value(value)}")
            return Status.FAILED

        if human_name:
            name = human_name.strip()
            name_split = name.split(" ")
            if len(name_split) > 2 or len(name_split) == 1:
                first_name = "<first-name-unset>"
                last_name = name
            else:
                first_name = name_split[0].strip()
                last_name = name_split[1].strip()
        else:
            name = "<first-name-unset> <last-name-unset>"
            first_name = "<first-name-unset>"
            last_name = "<last-name-unset>"
        try:
            user_args = {
                "first_name": first_name,
                "last_name": last_name,
                "full_name": name,
                "gecos": name,
                "display_name": name,
                "home_directory": f"/home/fedora/{username}",
                "disabled": status != "active",
                "mail": email,
                "ipasshpubkey": [k.strip() for k in ssh_key.split("\n") if k.strip()] if ssh_key else None,
                "fasircnick": ircnick.strip() if ircnick else None,
                "faslocale": locale.strip() if locale else None,
                "fastimezone": timezone.strip() if timezone else None,
                "fasgpgkeyid": [gpg_keyid[:16].strip()] if gpg_keyid else None,
                "fasstatusnote": status.strip(),
                "fasisprivate": bool(privacy),
                "fascreationtime": CREATION_TIME_RE.sub(r"\1Z", creation),
            }
            try:
                user_add_args = user_args.copy()
                # If they haven't synced yet, they must reset their password:
                user_add_args["random_pass"] = True
                user_add_args["faslocale"] = user_add_args["faslocale"] or "en_US"
                user_add_args["fastimezone"] = user_add_args["fastimezone"] or "UTC"
                self.ipa.user_add(username, **user_add_args)
                return Status.ADDED
            except python_freeipa.exceptions.FreeIPAError as e:
                if e.message == f'user with name "{username}" already exists':
                    # Don't overwrite first/last/full name with unset placeholders
                    if user_args["first_name"] == "<first-name-unset>":
                        del user_args["first_name"]
                    if user_args["last_name"] == "<last-name-unset>":
                        del user_args["last_name"]
                    if user_args["full_name"] == "<first-name-unset> <last-name-unset>":
                        del user_args["full_name"]

                    # Avoid resetting already set fields
                    if user_args["faslocale"] is None or user_args["fastimezone"] is None:
                        ipa_user = self.user_show(username)
                        if not user_args["faslocale"]:
                            user_args["faslocale"] = ipa_user.get("faslocale") or "en_US"
                        if not user_args["fastimezone"]:
                            user_args["fastimezone"] = ipa_user.get("fastimezone") or "UTC"

                    user_args = {
                        k: v
                        for k, v in user_args.items()
                        if v is not None and (not isinstance(v, str) or v.strip())
                    }

                    # Update them instead
                    self.ipa.user_mod(username, **user_args)
                    return Status.UPDATED
                else:
                    raise

        except python_freeipa.exceptions.Unauthorized:
            self.ipa.login(
                self.config["ipa"]["username"], self.config["ipa"]["password"]
            )
            return self.migrate_user(person_orig)
        except python_freeipa.exceptions.FreeIPAError as e:
            if e.message != "no modifications to be performed":
                print(e)
                return Status.FAILED
            return Status.UNMODIFIED
        except Exception as e:
            print(e)
            return Status.FAILED

    def add_users_to_groups(self, groups_to_users, category):
        if self.config["skip_user_membership"]:
            return

        if category not in ["members", "sponsors"]:
            raise ValueError("title must be eigher member or sponsor")

        click.echo(f"Adding {category} to groups")
        total = sum([len(members) for members in groups_to_users.values()])
        if total == 0:
            click.echo("Nothing to do.")
            return
        counter = 0
        with progressbar.ProgressBar(max_value=total, redirect_stdout=True) as bar:
            for group in sorted(groups_to_users):
                members = groups_to_users[group]
                for chunk in self.chunks(members):
                    counter += len(chunk)
                    self.check_reauth(counter)
                    added = set(chunk)
                    try:
                        if category == "members":
                            self.ipa.group_add_member(
                                group, chunk, no_members=True,
                            )
                        elif category == "sponsors":
                            result = self.ipa._request(
                                "group_add_member_manager", group, {"user": chunk},
                            )
                            if result["failed"]["membermanager"]["user"]:
                                raise python_freeipa.exceptions.ValidationError(
                                    result["failed"]
                                )
                    except python_freeipa.exceptions.ValidationError as e:
                        errors = []
                        for member_type in ("member", "membermanager"):
                            try:
                                errors.extend(e.message[member_type]["user"])
                            except KeyError:
                                continue
                        for msg in errors:
                            if msg[1] == "This entry is already a member":
                                added.remove(msg[0])
                            else:
                                print_status(
                                    Status.FAILED,
                                    f"Failed to add {msg[0]} in the {category} of {group}: "
                                    + msg[1],
                                )
                    except python_freeipa.exceptions.NotFound as e:
                        print_status(
                            Status.FAILED,
                            f"Failed to add {chunk} in the {category} of {group}: {e}",
                        )
                    except Exception as e:
                        print_status(
                            Status.FAILED,
                            f"Failed to add {chunk} in the {category} of {group}: {e}",
                        )
                    else:
                        if added:
                            print_status(
                                Status.ADDED,
                                f"Added {category} to {group}: {', '.join(sorted(added))}",
                            )
                    finally:
                        bar.update(counter)

    def remove_users_from_groups(self, groups_to_users):
        if self.config["skip_user_membership"]:
            return

        click.echo("Removing unapproved users from groups")
        total = sum([len(members) for members in groups_to_users.values()])
        if total == 0:
            click.echo("Nothing to do.")
            return
        counter = 0
        with progressbar.ProgressBar(max_value=total, redirect_stdout=True) as bar:
            for group in sorted(groups_to_users):
                members = groups_to_users[group]
                for chunk in self.chunks(members):
                    counter += len(chunk)
                    self.check_reauth(counter)
                    removed = set(chunk[:])
                    try:
                        self.ipa.group_remove_member(
                            self.config["groups"]["prefix"] + group,
                            chunk,
                            no_members=True,
                        )
                    except python_freeipa.exceptions.ValidationError as e:
                        errors = []
                        for member_type in ("member", "membermanager"):
                            try:
                                errors.extend(e.message[member_type]["user"])
                            except KeyError:
                                continue
                        for msg in errors:
                            if msg[1] == "This entry is not a member":
                                removed.remove(msg[0])
                            else:
                                print_status(
                                    Status.FAILED,
                                    f"Failed to remove {msg[0]} from {group}: "
                                    + msg[1],
                                )
                    except python_freeipa.exceptions.NotFound as e:
                        print_status(
                            Status.FAILED,
                            f"Failed to remove {chunk} from {group}: {e}",
                        )
                    except Exception as e:
                        print_status(
                            Status.FAILED,
                            f"Failed to remove {chunk} from {group}: {e}",
                        )
                    else:
                        if removed:
                            print_status(
                                Status.REMOVED,
                                f"Removed from {group}: {', '.join(sorted(removed))}",
                            )
                    finally:
                        bar.update(counter)

    def find_user_conflicts(
        self, fas_users: Dict[str, List[Dict]]
    ) -> Dict[str, List[str]]:
        """Compare users from different FAS instances and flag conflicts."""
        click.echo("Checking for conflicts between users from different FAS instances")

        users_to_conflicts = {}

        # email domains per FAS name and reverse
        fas_email_domains = {
            fas_name: fas_conf["email_domain"]
            for fas_name, fas_conf in self.config["fas"].items()
            if "email_domain" in fas_conf
        }
        email_domains_fas = {v: k for k, v in fas_email_domains.items()}

        # FAS users by instance, by name
        fas_users_by_name = {}

        # Map duplicate usernames to fas instance names
        usernames_to_check_for_fas = defaultdict(set)

        for fas_name, user_objs in fas_users.items():
            users_by_name = fas_users_by_name[fas_name] = {
                user_obj["username"]: user_obj for user_obj in user_objs
            }

            for other_fas_name, other_user_objs in fas_users.items():
                if other_fas_name in fas_users_by_name:
                    continue

                other_user_names = {uobj["username"] for uobj in other_user_objs}

                usernames_to_check = set(users_by_name) & other_user_names
                for name in usernames_to_check:
                    usernames_to_check_for_fas[name] |= {fas_name, other_fas_name}

        # Check users existing in different FAS instances
        for username, fas_names in sorted(
            usernames_to_check_for_fas.items(), key=lambda x: x[0]
        ):
            user_conflicts = defaultdict(list)

            fas_to_user_obj = {
                fas_name: fas_users_by_name[fas_name][username]
                for fas_name in fas_names
            }

            email_addresses_to_fas = defaultdict(set)
            for fas_name, user_obj in fas_to_user_obj.items():
                email_addresses_to_fas[user_obj["email"]].add(fas_name)

            for email_address, fas_names in email_addresses_to_fas.items():
                mailbox, domain = email_address.rsplit("@", 1)
                domain_fas_name = email_domains_fas.get(domain)
                if mailbox == username and domain_fas_name:
                    if domain_fas_name in fas_names:
                        user_conflicts["circular_email"].append(
                            {
                                "fas_name": domain_fas_name,
                                "email_address": email_address,
                            }
                        )
                        fas_names.remove(domain_fas_name)

                    if fas_names:
                        user_conflicts["email_pointing_to_other_fas"].append(
                            {
                                "tgt_fas_name": domain_fas_name,
                                "email_address": email_address,
                                "src_fas_names": set(fas_names),  # make a copy
                            }
                        )
                        fas_names.clear()

            if (
                len(
                    list(
                        e
                        for e, fas_names in email_addresses_to_fas.items()
                        if fas_names
                    )
                )
                > 1
            ):
                for email_address, fas_names in email_addresses_to_fas.items():
                    user_conflicts["email_address_conflicts"].append(
                        {"email_address": email_address, "fas_names": fas_names}
                    )

            if user_conflicts:
                users_to_conflicts[username] = user_conflicts

        click.echo("Done checking user conflicts.")

        return users_to_conflicts
