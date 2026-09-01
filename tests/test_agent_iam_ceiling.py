"""The IAM ceiling the agent GSA is provisioned with.

GKE authorizes a request if EITHER IAM or Kubernetes RBAC allows it. The agent's
Kubernetes RBAC is read-only, but that constrains only one half of the union: a
GSA holding `roles/container.admin` is authorized by IAM no matter how narrow the
KSA is. `roles/container.admin` is also the one predefined GKE role carrying
`container.clusters.impersonate`, and GKE grants IAM roles at the project level,
so that impersonation reaches every cluster in the project and the grant cannot
be narrowed to one.

The install used to offer that as one word, `PLATFORM_AGENT_PERMISSION_SET=gke-admin`.
These tests are what keeps it gone, across the whole of the one install engine:
the shell front doors that collect the answer (`install.sh`, `installer_common.sh`,
`common.sh`) and the Terraform composition that turns it into IAM bindings
(`terraform/examples/full-install`). The shell half runs the real bash rather
than grepping for a string, so re-adding the bundle under a different arm name
is caught by the accepted-value test even if the role list is spelled
differently. The Terraform half is read as text — `terraform` is not a
dependency of this suite — but it is read structurally, so a bundle re-added
under any local name fails the forbidden-role check.

`custom` remains, so a deployment that needs broad roles still has a path; it just
has to name each role. That is deliberately not tested as "safe" — a `custom` set
can grant anything. What is tested is that no *built-in* set does.

Run:
  python3 -m unittest discover -s tests -p 'test_agent_iam_ceiling.py' -v
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "k8s-operator" / "scripts"
COMMON_SH = SCRIPTS / "common.sh"
INSTALLER_COMMON_SH = SCRIPTS / "installer_common.sh"
INSTALL_SH = REPO_ROOT / "install.sh"
TERRAFORM_DIR = REPO_ROOT / "terraform"
FULL_INSTALL = TERRAFORM_DIR / "examples" / "full-install"
TF_MAIN = FULL_INSTALL / "main.tf"
TF_VARIABLES = FULL_INSTALL / "variables.tf"

# The exact set the composition grants for `read-only`. Written out rather than
# derived so that widening it is a visible diff here too.
READ_ONLY_ROLES = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
]

# Roles no built-in permission set may grant the agent GSA. The first two are the
# structural ones (IAM-side authorization that outranks RBAC, plus unscopable
# impersonation); the rest were in the removed bundle and would come back with it.
FORBIDDEN_ROLES = {
    "roles/container.admin",
    "roles/container.clusterAdmin",
    "roles/container.developer",
    "roles/container.hostServiceAgentUser",
    "roles/monitoring.admin",
    "roles/logging.admin",
    "roles/owner",
    "roles/editor",
    "roles/iam.serviceAccountTokenCreator",
}

# Values a human or a stale vars.sh might plausibly carry. Everything here that
# is not `read-only` or `custom` must be rejected outright.
REJECTED_VALUES = [
    "gke-admin",
    "GKE-ADMIN",
    "  gke-admin  ",
    "gke_admin",
    "admin",
    "cluster-admin",
    "gke-owner",
    "readonly",
]

ACCEPTED_VALUES = ["read-only", "custom"]


def _run_bash(script: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Run `script` with common.sh already sourced, in a throwaway state dir.

    CI=1 makes `init_var` take defaults instead of blocking on a prompt, and
    VARS_FILE points at a temp file so nothing touches the developer's real
    (git-ignored) k8s-operator/scripts/vars.sh. TERM=dumb keeps common.sh's
    EXIT trap (`tput cnorm`) from writing cursor escapes into the stdout the
    assertions are read from.
    """
    with tempfile.TemporaryDirectory() as state_dir:
        env = dict(os.environ)
        env.pop("PLATFORM_AGENT_PERMISSION_SET", None)
        env.pop("PLATFORM_AGENT_CUSTOM_ROLES", None)
        env.update(
            {
                "CI": "1",
                "TERM": "dumb",
                "SCRIPT_DIR": str(SCRIPTS),
                "VARS_FILE": str(Path(state_dir) / "vars.sh"),
            }
        )
        env.update(env_overrides)
        return subprocess.run(
            ["bash", "-c", f'source "$SCRIPT_DIR/common.sh"\n{script}'],
            capture_output=True,
            text=True,
            env=env,
        )


def _terraform_list_locals(source: str) -> dict[str, list[str]]:
    """Every `name = [ "…", "…" ]` list assignment in a Terraform file.

    Enough HCL for what this asserts and no more: the role bundles are flat
    lists of quoted strings. Comment lines inside a list are skipped, which is
    why the entries are pulled out by their quotes rather than by splitting on
    commas.
    """
    lists: dict[str, list[str]] = {}
    for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\[(.*?)\]", source, re.S | re.M):
        name, body = match.group(1), match.group(2)
        if "\n" not in body and '"' not in body:
            continue
        entries = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            entries.extend(re.findall(r'"([^"]*)"', stripped))
        lists[name] = entries
    return lists


class PermissionSetValidatorTest(unittest.TestCase):
    """`init_var_platform_agent_permission_set` is the provisioning entry point."""

    def _validate(self, value: str) -> subprocess.CompletedProcess:
        return _run_bash(
            "init_var_platform_agent_permission_set",
            {"PLATFORM_AGENT_PERMISSION_SET": value},
        )

    def test_gke_admin_is_rejected(self):
        result = self._validate("gke-admin")
        self.assertNotEqual(
            0,
            result.returncode,
            "PLATFORM_AGENT_PERMISSION_SET=gke-admin must fail provisioning; it grants "
            "roles/container.admin, which authorizes the agent through IAM regardless "
            "of its Kubernetes RBAC.\n" + result.stdout + result.stderr,
        )

    def test_gke_admin_says_why_rather_than_just_invalid(self):
        """A cached vars.sh from before the removal has to be diagnosable."""
        combined = self._validate("gke-admin")
        self.assertIn("has been removed", combined.stdout + combined.stderr)

    def test_only_read_only_and_custom_are_accepted(self):
        for value in REJECTED_VALUES:
            with self.subTest(value=value):
                self.assertNotEqual(
                    0,
                    self._validate(value).returncode,
                    f"{value!r} must not be an accepted permission set",
                )

    def test_read_only_is_accepted(self):
        self.assertEqual(0, self._validate("read-only").returncode)

    def test_custom_is_accepted_when_roles_are_named(self):
        result = _run_bash(
            "init_var_platform_agent_permission_set",
            {
                "PLATFORM_AGENT_PERMISSION_SET": "custom",
                "PLATFORM_AGENT_CUSTOM_ROLES": "roles/container.viewer",
            },
        )
        self.assertEqual(
            0,
            result.returncode,
            "`custom` is the documented path for a deployment that needs broader "
            "roles; removing it would leave no supported alternative.\n"
            + result.stdout
            + result.stderr,
        )

    def test_the_prompt_does_not_offer_a_set_the_validator_rejects(self):
        """The prompt string is the operator-facing list; it has to match."""
        prompt = re.search(
            r'init_var "PLATFORM_AGENT_PERMISSION_SET" [^\n]*"([^"]*)"',
            COMMON_SH.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(prompt, "the permission-set prompt moved or was renamed")
        self.assertNotIn("gke-admin", prompt.group(1))


class SharedValidatorTest(unittest.TestCase):
    """The rule itself, in installer_common.sh, executed rather than read.

    install.sh, uninstall.sh and upgrade.sh source installer_common.sh rather
    than the whole provisioning helper, so this is the one place the rule can
    live and still reach every front door. A regex rewritten to keep the arm
    under another spelling would still have to fail here.
    """

    def _source(self, call: str) -> subprocess.CompletedProcess:
        # installer_common.sh's contract is that the caller supplies the
        # reporters; stub them so the message lands on stdout to assert on.
        harness = (
            "print_error() { echo \"$*\"; }\n"
            "print_warning() { echo \"$*\"; }\n"
            "print_info() { echo \"$*\"; }\n"
            f'source "{INSTALLER_COMMON_SH}"\n{call}'
        )
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)

    def test_the_accepted_values_are_exactly_read_only_and_custom(self):
        for value in ACCEPTED_VALUES:
            with self.subTest(value=value):
                self.assertEqual(
                    0, self._source(f'is_valid_permission_set "{value}"').returncode
                )
        for value in REJECTED_VALUES:
            with self.subTest(value=value):
                self.assertNotEqual(
                    0,
                    self._source(f'is_valid_permission_set "{value}"').returncode,
                    f"{value!r} must be rejected",
                )

    def test_the_shared_gate_explains_the_removal(self):
        result = self._source('require_supported_permission_set "gke-admin"')
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("has been removed", result.stdout + result.stderr)

    def test_the_explanation_survives_the_spellings_a_stale_config_carries(self):
        """Not covered by the rejected-values test above, which only checks the exit code.

        common.sh trims and lowercases before calling the gate; install.sh
        passes `--permission-set` through raw. So the two front doors disagreed
        about which spellings reach the named arm, and the one that did not was
        the flag -- the path a GitHub environment variable or a hand-edited
        vars.sh arrives on. A non-zero exit is not the point of this function;
        the explanation is.
        """
        for value in ["gke-admin", "GKE-ADMIN", "Gke-Admin", "  gke-admin  ", "gke-admin\t"]:
            with self.subTest(value=value):
                result = self._source(f'require_supported_permission_set "{value}"')
                combined = result.stdout + result.stderr
                self.assertNotEqual(0, result.returncode, combined)
                self.assertIn(
                    "has been removed",
                    combined,
                    f"{value!r} was rejected, but with the generic error rather than the "
                    "explanation -- so a cached config reads as a typo",
                )

    def test_a_custom_role_list_reaching_the_removed_ceiling_is_flagged(self):
        """`custom` is the supported way to widen; it should not be the quiet way.

        `--custom-roles="roles/container.admin"` lands in a machine-generated
        terraform.tfvars that nobody opens, so on the installer path the
        "naming each role puts it in front of a reviewer" argument does not
        hold on its own. The warning is what puts it in front of someone. It
        does not refuse -- an operator entitled to that grant still gets it.
        """
        for roles in [
            "roles/container.admin",
            "roles/container.viewer,roles/container.admin",
            "roles/container.viewer roles/owner",
        ]:
            with self.subTest(roles=roles):
                result = self._source(f'warn_on_overreaching_custom_roles "{roles}"')
                self.assertEqual(0, result.returncode, "this warns, it does not refuse")
                self.assertIn("GKE authorizes on either", result.stdout + result.stderr)

    def test_a_benign_custom_role_list_is_not_flagged(self):
        """A warning that fires on everything is a warning nobody reads."""
        result = self._source(
            'warn_on_overreaching_custom_roles "roles/container.viewer roles/logging.viewer"'
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("", (result.stdout + result.stderr).strip())

    def test_the_warning_covers_every_role_the_ceiling_test_forbids(self):
        """One list, two homes -- so assert they are the same list.

        The shell cannot import FORBIDDEN_ROLES, so the drift this catches is
        real: a role added here and not there leaves a set the tests forbid but
        the installer waves through.
        """
        declared = re.search(
            r'^OVERREACHING_AGENT_ROLES="([^"]*)"',
            INSTALLER_COMMON_SH.read_text(encoding="utf-8"),
            re.M,
        )
        self.assertIsNotNone(declared, "OVERREACHING_AGENT_ROLES moved or was renamed")
        self.assertEqual(FORBIDDEN_ROLES, set(declared.group(1).split()))

    def test_the_shared_gate_still_accepts_what_remains(self):
        for value in ACCEPTED_VALUES:
            with self.subTest(value=value):
                result = self._source(f'require_supported_permission_set "{value}"')
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class InstallerFrontDoorTest(unittest.TestCase):
    """install.sh is the operator-facing surface; it must not offer what is gone.

    A menu entry or a `--help` line naming a set the validator rejects is worse
    than a missing one: the operator picks it, and the install dies four screens
    later on a value the installer itself suggested.
    """

    def test_the_installer_never_names_the_removed_set(self):
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(INSTALL_SH.read_text(encoding="utf-8").splitlines(), 1)
            if "gke-admin" in line
        ]
        self.assertEqual(
            [],
            offenders,
            "install.sh still names the removed `gke-admin` permission set",
        )

    def test_the_installer_normalises_before_it_branches_on_the_value(self):
        """The gate normalising its own argument is not enough for the caller.

        `require_supported_permission_set` lowercases what it is handed so that
        every spelling reaches the right message, but it cannot fix the
        variable in the caller. Everything downstream of the call in install.sh
        compares against the lowercase literal -- the custom-roles requirement,
        the over-reach warning, the two `write_state_var` lines, and (through
        the generated tfvars) terraform's case-sensitive `contains()` on
        permission_set. So a `--permission-set=Custom` that cleared the gate
        and stayed `Custom` would miss all of them and fail much later, in the
        apply, with an error about a different value entirely. The fix is one
        line and this is what holds it there.
        """
        source = INSTALL_SH.read_text(encoding="utf-8")
        assignment = re.search(
            r'^\s*local permission_set="\$\{PARAM_PERMISSION_SET:-read-only\}"$',
            source,
            re.M,
        )
        self.assertIsNotNone(assignment, "the permission_set assignment moved or was renamed")
        gate = source.index('require_supported_permission_set "$permission_set"')
        between = source[assignment.end() : gate]
        normalisation = [
            line.strip()
            for line in between.splitlines()
            if line.strip().startswith("permission_set=")
        ]
        self.assertEqual(
            1,
            len(normalisation),
            "install.sh does not reassign permission_set between reading the flag and "
            "validating it, so a mixed-case value clears the gate and then misses every "
            "comparison after it",
        )

        # Executed, not just matched: a line that reassigns the variable without
        # actually lowercasing it would satisfy the check above.
        probe = subprocess.run(
            [
                "bash",
                "-c",
                f'permission_set="$1"\n{normalisation[0]}\nprintf "%s" "$permission_set"',
                "_",
                "  CuStOm  ",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual("custom", probe.stdout, probe.stdout + probe.stderr)

    def test_the_installer_routes_its_check_through_the_shared_gate(self):
        """Not a duplicate of the shell test above: this is the wiring.

        install.sh can only produce the explanatory refusal if it calls the
        function that carries it. A second copy of the rule inlined here is
        exactly the drift the shared helper exists to prevent.
        """
        self.assertIn(
            "require_supported_permission_set",
            INSTALL_SH.read_text(encoding="utf-8"),
            "install.sh validates the permission set some other way, so a cached "
            "`gke-admin` reaches it without the explanation",
        )


class TerraformRoleBundlesTest(unittest.TestCase):
    """The composition is what actually creates the IAM bindings."""

    def setUp(self):
        self.main = TF_MAIN.read_text(encoding="utf-8")
        self.lists = _terraform_list_locals(self.main)

    def test_the_read_only_bundle_is_unchanged(self):
        self.assertIn(
            "read_only_roles", self.lists, "the read-only role bundle moved or was renamed"
        )
        self.assertEqual(READ_ONLY_ROLES, self.lists["read_only_roles"])

    def test_no_role_bundle_in_the_composition_grants_a_forbidden_role(self):
        for name, roles in self.lists.items():
            granted = {r for r in roles if r.startswith("roles/")}
            with self.subTest(local=name):
                self.assertEqual(
                    set(),
                    granted & FORBIDDEN_ROLES,
                    f"local.{name} grants a role that authorizes the agent through IAM "
                    "independently of its Kubernetes RBAC",
                )

    def test_the_permission_set_branch_names_no_removed_bundle(self):
        offenders = [
            f"main.tf:{n}: {line.strip()}"
            for n, line in enumerate(self.main.splitlines(), 1)
            if "gke-admin" in line or "gke_admin" in line
        ]
        self.assertEqual(
            [],
            offenders,
            "main.tf still branches on, or still defines, the removed `gke-admin` "
            "permission set",
        )

    def test_the_grant_list_does_not_branch_on_the_permission_set_at_all(self):
        """Defence in depth for a caller that skipped the variable validation.

        `terraform console` does not evaluate variable validations, and a
        `-var permission_set=...` from a script bypasses the shell gate too.
        What makes that harmless is structural rather than defensive: with one
        bundle left, `agent_project_roles` reads `project_roles` or the
        read-only bundle and never looks at `permission_set`, so no value of it
        -- removed, misspelled, or invented later -- can select an admin list.
        Re-introducing the branch is what this catches, and it is the assertion
        that would have to be deleted to bring the bundle back quietly. The
        forbidden-role sweep below does not cover it: a branch reintroduced
        against a benign list passes that one and leaves the shape behind.
        """
        start = re.search(r"^[ \t]*agent_project_roles[ \t]*=", self.main, re.M)
        self.assertIsNotNone(start, "local.agent_project_roles moved or was renamed")
        # The assignment runs until its brackets balance and the line ends, so
        # read forward from the `=` rather than to the first newline -- a
        # parenthesised conditional spans several lines, which is exactly the
        # shape being looked for.
        text, depth = "", 0
        for ch in self.main[start.end() :]:
            text += ch
            depth += (ch in "([") - (ch in ")]")
            if ch == "\n" and depth <= 0:
                break
        self.assertTrue(text.strip(), "could not read the agent_project_roles expression")
        self.assertNotIn(
            "var.permission_set",
            text,
            "the agent's role list branches on permission_set again; a value that "
            "evades the validation can then choose the branch",
        )

    def test_the_variable_accepts_only_the_two_remaining_sets(self):
        variables = TF_VARIABLES.read_text(encoding="utf-8")
        condition = re.search(
            r'contains\(\s*\[([^\]]*)\],\s*var\.permission_set\s*\)', variables
        )
        self.assertIsNotNone(
            condition, "variables.tf no longer validates permission_set against a fixed list"
        )
        self.assertEqual(
            ACCEPTED_VALUES, re.findall(r'"([^"]*)"', condition.group(1))
        )


class NoShippedInstallPathGrantsContainerAdminTest(unittest.TestCase):
    """The catch-all: whatever the shape of the engine, nothing hands the agent admin.

    Scoped to what an install actually runs — the Terraform composition and
    modules, and the three shell front doors. Deliberately not the whole
    repository: the evaluation fleet under bench/ and the developer bootstrap
    under k8s-operator/scripts/dev/ grant admin roles to identities that are
    not the agent, and folding them in here would either fail permanently or
    have to be excepted by name.

    Comments and diagnostics are excluded, because the refusal has to name what
    it refuses. `roles/container.admin` appears in the Terraform validation's
    error_message and in the installer's refusal text on purpose: an operator
    whose cached configuration names the removed set gets told why, and a
    message that cannot say the role name is a message that explains nothing.
    What this looks for is a *grant* — a role in a list the composition binds,
    or a value a front door would accept.

    One consequence to know rather than discover: every line of
    `terraform.tfvars.example` is commented out, being a template to copy, so
    that file contributes nothing here. It is not a hole — a value uncommented
    out of it still meets the `permission_set` validation in `variables.tf`,
    which `TerraformRoleBundlesTest` covers — but do not read a pass as saying
    the example was checked.
    """

    _DIAGNOSTIC = re.compile(
        r"^\s*(#|//)|error_message\s*=|description\s*=|print_(error|warning|info)\b"
    )

    # The two places a forbidden role is named in order to be refused rather
    # than granted, exempted by name so the exemption is visible in the diff
    # that adds it. Both are executed by SharedValidatorTest, so nothing here
    # goes unchecked -- what is waived is the text sweep, not the behaviour.
    _DENYLIST_EXEMPT = (
        r'^OVERREACHING_AGENT_ROLES="[^"]*"',  # the whole line, not just the name
        r"^require_supported_permission_set\(\) \{.*?^\}$",
        r"^warn_on_overreaching_custom_roles\(\) \{.*?^\}$",
    )

    def _exempt_lines(self) -> set[str]:
        source = INSTALLER_COMMON_SH.read_text(encoding="utf-8")
        exempt: set[str] = set()
        for pattern in self._DENYLIST_EXEMPT:
            match = re.search(pattern, source, re.M | re.S)
            self.assertIsNotNone(
                match, f"the refusal machinery matching {pattern!r} moved or was renamed"
            )
            exempt.update(match.group(0).splitlines())
        return exempt

    def _shipped_install_sources(self) -> list[Path]:
        paths = sorted(TERRAFORM_DIR.rglob("*.tf"))
        paths += sorted(TERRAFORM_DIR.rglob("*.tfvars.example"))
        paths += [INSTALL_SH, INSTALLER_COMMON_SH, COMMON_SH]
        return paths

    def _effective_lines(self, path: Path):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if self._DIAGNOSTIC.search(line):
                continue
            yield n, line

    # roles/iam.serviceAccountTokenCreator is forbidden at project scope —
    # there it lets the agent mint a token for any service account in the
    # project — and bounded only when bound on a single *pool member* as the
    # resource, which is how the scoped pool grants it so that
    # impersonated_credentials can mint at all
    # (terraform/modules/kube-agents-iam/scoped_pool.tf). So the suppression
    # is scope- and target-aware rather than by name: the role is waived only
    # on lines inside a `resource "google_service_account_iam_member"` block
    # whose body binds on `google_service_account.scoped`. Member scope alone
    # is deliberately not enough — tokenCreator bound on a wider account (an
    # operator GSA, a token signer) is escalation to that identity, and a
    # future member-scoped grant on anything but the pool fails the sweep and
    # has to argue its way in here. A project-scoped reintroduction —
    # google_project_iam_member, a role bundle, an installer list — is still
    # caught, and the role stays in FORBIDDEN_ROLES.
    _MEMBER_SCOPED_RESOURCE = re.compile(
        r'^\s*resource\s+"google_service_account_iam_member"'
    )
    # The attribute assignment itself, not any mention: a comment naming the
    # pool, or a member= line, must not be what waives the block. Comments are
    # stripped before matching for the same reason.
    _POOL_MEMBER_TARGET = re.compile(
        r"\bservice_account_id\s*=\s*google_service_account\.scoped\b"
    )

    @classmethod
    def _binds_on_pool_member(cls, line: str) -> bool:
        code = re.split(r"#|//", line, 1)[0]
        return cls._POOL_MEMBER_TARGET.search(code) is not None

    @classmethod
    def _member_scoped_lines(cls, text: str) -> set[int]:
        """Line numbers inside member-scoped blocks that bind on a pool member.

        The declaration line counts as part of the block, so a one-line
        `resource ... { ... }` is scoped to exactly its own line rather than
        leaking the waiver onto the line after it.
        """
        lines: set[int] = set()
        block: list[tuple[int, str]] = []
        inside = False
        depth = 0
        for n, line in enumerate(text.splitlines(), 1):
            if not inside:
                if not cls._MEMBER_SCOPED_RESOURCE.search(line):
                    continue
                inside = True
                depth = 0
            block.append((n, line))
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                inside = False
                if any(cls._binds_on_pool_member(text) for _, text in block):
                    lines.update(number for number, _ in block)
                block = []
        return lines

    def test_no_shipped_install_source_grants_a_forbidden_role(self):
        exempt = self._exempt_lines()
        offenders = []
        for path in self._shipped_install_sources():
            member_scoped: set[int] = set()
            if path.suffix == ".tf":
                member_scoped = self._member_scoped_lines(
                    path.read_text(encoding="utf-8")
                )
            for n, line in self._effective_lines(path):
                for role in FORBIDDEN_ROLES:
                    if role not in line or line in exempt:
                        continue
                    if (
                        role == "roles/iam.serviceAccountTokenCreator"
                        and n in member_scoped
                    ):
                        continue
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: {role}")
        self.assertEqual(
            [],
            sorted(offenders),
            "a shipped install path grants a role that authorizes the agent through "
            "IAM regardless of its Kubernetes RBAC",
        )

    def test_the_token_creator_suppression_is_scope_and_target_aware(self):
        # The suppression must turn on the resource type AND the bound
        # account. All three refused snippets grant the same role; only the
        # member-scoped grant on a pool member is waived, so widening the
        # suppression to a name match, a file match, or member-scope-alone
        # fails here before it can hide a wider grant.
        pool_member = (
            'resource "google_service_account_iam_member" "x" {\n'
            "  service_account_id = google_service_account.scoped[each.key].name\n"
            '  role               = "roles/iam.serviceAccountTokenCreator"\n'
            "}\n"
        )
        self.assertIn(3, self._member_scoped_lines(pool_member))
        # Member-scoped, but bound on an account that is not a pool member:
        # tokenCreator there is escalation to that identity, not a bounded
        # pool grant, and it must stay flagged.
        wider_target = pool_member.replace(
            "google_service_account.scoped[each.key].name",
            "google_service_account.agent.name",
        )
        self.assertEqual(set(), self._member_scoped_lines(wider_target))
        project_scoped = (
            'resource "google_project_iam_member" "x" {\n'
            '  role = "roles/iam.serviceAccountTokenCreator"\n'
            "}\n"
        )
        self.assertEqual(set(), self._member_scoped_lines(project_scoped))
        # A mention is not a binding: a comment naming the pool, or a member=
        # line referencing it, must not waive a block that binds elsewhere.
        commented = (
            'resource "google_service_account_iam_member" "x" {\n'
            "  # narrower than google_service_account.scoped would be\n"
            "  service_account_id = google_service_account.agent.name\n"
            '  role               = "roles/iam.serviceAccountTokenCreator"\n'
            "}\n"
        )
        self.assertEqual(set(), self._member_scoped_lines(commented))
        # The waiver ends with the block: the same role one line after the
        # closing brace is not waived.
        trailing = pool_member + 'role = "roles/iam.serviceAccountTokenCreator"\n'
        self.assertNotIn(5, self._member_scoped_lines(trailing))
        # A one-line block is scoped to exactly its own line — the waiver
        # covers an inline grant and does not leak onto the next line.
        one_line = (
            'resource "google_service_account_iam_member" "x" '
            "{ service_account_id = google_service_account.scoped[k].name, "
            'role = "roles/iam.serviceAccountTokenCreator" }\n'
            'role = "roles/iam.serviceAccountTokenCreator"\n'
        )
        waived = self._member_scoped_lines(one_line)
        self.assertIn(1, waived)
        self.assertNotIn(2, waived)

    def test_the_pool_grant_is_what_the_suppression_waives(self):
        # The suppression exists for exactly one shipped line. Pin that it is
        # still needed and still covered: the pool's grant sits inside a
        # member-scoped block, so if the resource is retyped or the grant
        # moves out of it, this fails alongside the sweep rather than leaving
        # a dead waiver behind.
        pool = (
            TERRAFORM_DIR / "modules" / "kube-agents-iam" / "scoped_pool.tf"
        ).read_text(encoding="utf-8")
        waived = self._member_scoped_lines(pool)
        grant_lines = [
            n
            for n, line in enumerate(pool.splitlines(), 1)
            if "roles/iam.serviceAccountTokenCreator" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            1, len(grant_lines), "scoped_pool.tf should grant tokenCreator exactly once"
        )
        self.assertIn(grant_lines[0], waived)

    def test_no_shipped_install_source_offers_the_removed_permission_set(self):
        # The refusal itself has to compare against the literal to recognise
        # it. SharedValidatorTest executes that function, so nothing here is
        # going unchecked.
        exempt = self._exempt_lines()

        offenders = [
            f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}"
            for path in self._shipped_install_sources()
            for n, line in self._effective_lines(path)
            if "gke-admin" in line and line not in exempt
        ]
        self.assertEqual(
            [],
            sorted(offenders),
            "a shipped install path still treats `gke-admin` as a value rather than "
            "as something to refuse",
        )


if __name__ == "__main__":
    unittest.main()
