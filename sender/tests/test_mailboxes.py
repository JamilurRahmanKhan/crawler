import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db as db_module
import pytest
from mailboxes import Mailbox, load_mailboxes, pick_mailbox


@pytest.fixture
def conn(tmp_path):
    c = db_module.get_connection(str(tmp_path / "test.db"))
    db_module.init_db(c)
    yield c
    c.close()


def _mb(name, warmup_start=date(2026, 1, 1), daily_cap_override=None):
    return Mailbox(
        name=name, smtp_host="smtp.x.com", smtp_port=587, smtp_user=name, smtp_password="pw",
        imap_host="imap.x.com", imap_port=993, from_name="Name", reply_to=name,
        warmup_start_date=warmup_start, daily_cap_override=daily_cap_override,
    )


# --- load_mailboxes ----------------------------------------------------------------

def test_load_mailboxes_parses_valid_file(tmp_path):
    path = tmp_path / "mailboxes.json"
    path.write_text(json.dumps([
        {"name": "a@x.com", "smtp_host": "smtp.x.com", "smtp_user": "a@x.com",
         "smtp_password": "pw", "warmup_start_date": "2026-01-01"},
    ]))
    mailboxes = load_mailboxes(str(path))
    assert len(mailboxes) == 1
    assert mailboxes[0].name == "a@x.com"
    assert mailboxes[0].imap_host == "imap.x.com"  # derived from smtp_host


def test_load_mailboxes_rejects_missing_required_field(tmp_path):
    path = tmp_path / "mailboxes.json"
    path.write_text(json.dumps([{"name": "a@x.com", "smtp_host": "smtp.x.com"}]))
    with pytest.raises(SystemExit):
        load_mailboxes(str(path))


def test_load_mailboxes_rejects_duplicate_names(tmp_path):
    path = tmp_path / "mailboxes.json"
    entry = {"name": "a@x.com", "smtp_host": "smtp.x.com", "smtp_user": "a@x.com",
              "smtp_password": "pw", "warmup_start_date": "2026-01-01"}
    path.write_text(json.dumps([entry, entry]))
    with pytest.raises(SystemExit):
        load_mailboxes(str(path))


def test_load_mailboxes_rejects_empty_array(tmp_path):
    path = tmp_path / "mailboxes.json"
    path.write_text("[]")
    with pytest.raises(SystemExit):
        load_mailboxes(str(path))


def test_load_mailboxes_respects_daily_cap_override(tmp_path):
    path = tmp_path / "mailboxes.json"
    path.write_text(json.dumps([
        {"name": "a@x.com", "smtp_host": "smtp.x.com", "smtp_user": "a@x.com",
         "smtp_password": "pw", "warmup_start_date": "2026-01-01", "daily_cap_override": 15},
    ]))
    mailboxes = load_mailboxes(str(path))
    assert mailboxes[0].daily_cap() == 15


def test_load_mailboxes_example_file_is_itself_valid():
    # the example file shipped in this repo must actually parse -- catches
    # doc/code drift if the schema changes but the example isn't updated
    example_path = Path(__file__).parent.parent / "mailboxes.example.json"
    mailboxes = load_mailboxes(str(example_path))
    assert len(mailboxes) >= 1


# --- pick_mailbox (load-balancing) --------------------------------------------------

def test_pick_mailbox_picks_most_remaining_capacity(conn):
    mb1 = _mb("a@x.com", daily_cap_override=10)
    mb2 = _mb("b@x.com", daily_cap_override=10)
    db_module.increment_mailbox_count(conn, "a@x.com", "2026-01-01")
    db_module.increment_mailbox_count(conn, "a@x.com", "2026-01-01")
    db_module.increment_mailbox_count(conn, "a@x.com", "2026-01-01")
    # a has sent 3/10 (7 remaining), b has sent 0/10 (10 remaining) -- pick b
    chosen = pick_mailbox(conn, [mb1, mb2], "2026-01-01", db_module.get_mailbox_count_today)
    assert chosen.name == "b@x.com"


def test_pick_mailbox_returns_none_when_all_at_cap(conn):
    mb1 = _mb("a@x.com", daily_cap_override=2)
    mb2 = _mb("b@x.com", daily_cap_override=1)
    for _ in range(2):
        db_module.increment_mailbox_count(conn, "a@x.com", "2026-01-01")
    db_module.increment_mailbox_count(conn, "b@x.com", "2026-01-01")
    chosen = pick_mailbox(conn, [mb1, mb2], "2026-01-01", db_module.get_mailbox_count_today)
    assert chosen is None


def test_pick_mailbox_respects_independent_warmup_stages(conn):
    # a warmed mailbox (started long ago) has a much bigger cap than a brand
    # new one -- picking by "most remaining" should favor the warmed one
    # when both are otherwise unused today
    old_mailbox = _mb("veteran@x.com", warmup_start=date(2020, 1, 1))  # steady state: cap 40
    new_mailbox = _mb("rookie@x.com", warmup_start=date(2026, 1, 1))    # day 0: cap 10
    chosen = pick_mailbox(conn, [old_mailbox, new_mailbox], "2020-06-01", db_module.get_mailbox_count_today)
    assert chosen.name == "veteran@x.com"


def test_pick_mailbox_single_mailbox_pool(conn):
    mb = _mb("only@x.com", daily_cap_override=5)
    chosen = pick_mailbox(conn, [mb], "2026-01-01", db_module.get_mailbox_count_today)
    assert chosen.name == "only@x.com"
