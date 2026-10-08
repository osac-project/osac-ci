import pytest

from osac_ci.model import Snapshot
from osac_ci.policy import Trust
from osac_ci.rules.fork import authorization_command, fork_secrets_authorized

pytestmark = pytest.mark.unit
SHA_BOUND = Trust(authorization="sha-bound")
HEAD = "a" * 40


def fork(**kw: object) -> Snapshot:
    base: dict[str, object] = {
        "repo": "o/r", "number": 1, "head_sha": HEAD, "is_fork": True, "author": "outsider", "fork_owner": "outsider",
    }  # fmt: skip
    base.update(kw)
    return Snapshot(**base)  # type: ignore[arg-type]


def test_a_branch_in_this_repository_is_trusted_in_both_modes() -> None:
    for trust in (Trust(), SHA_BOUND):
        assert fork_secrets_authorized(fork(is_fork=False), trust)


@pytest.mark.parametrize("trust", [Trust(), SHA_BOUND])
def test_an_org_member_author_or_member_fork_owner_needs_no_authorization(trust: Trust) -> None:
    assert fork_secrets_authorized(fork(author_is_org_member=True), trust)
    assert fork_secrets_authorized(fork(fork_owner="member", fork_owner_is_org_member=True), trust)
    # the author's own fork does not count twice
    assert not fork_secrets_authorized(fork(fork_owner_is_org_member=True), trust)


@pytest.mark.parametrize("trust", [Trust(), SHA_BOUND])
def test_an_allow_listed_bot_needs_no_authorization(trust: Trust) -> None:
    allowed = trust.model_copy(update={"trusted_bots": ("dependabot[bot]",)})
    assert fork_secrets_authorized(fork(author="dependabot[bot]"), allowed)
    assert not fork_secrets_authorized(fork(author="other[bot]"), allowed)


def test_label_mode_is_todays_behavior() -> None:
    assert not fork_secrets_authorized(fork())
    assert fork_secrets_authorized(fork(labels=frozenset({"ok-to-test"})))
    assert not fork_secrets_authorized(fork(authorized_by="reviewer"))


def test_sha_bound_mode_ignores_the_label_and_needs_an_authorizer_of_this_commit() -> None:
    assert not fork_secrets_authorized(fork(labels=frozenset({"ok-to-test"})), SHA_BOUND)
    assert fork_secrets_authorized(fork(authorized_by="reviewer"), SHA_BOUND)
    assert not fork_secrets_authorized(fork(), SHA_BOUND)


def test_the_command_to_post_names_the_full_commit() -> None:
    assert authorization_command(HEAD) == f"/ok-to-test {HEAD}"
