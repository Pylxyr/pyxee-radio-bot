
from conftest import run
from twitch_radio.maintenance import backup_once, list_backups, prune_backups


def test_backup_creates_a_restorable_copy_and_prunes(make_db, tmp_path):
    async def go():
        db = await make_db()
        await db.credit("1", "Alice", 42)
        out = tmp_path / "backups"
        first = await backup_once(db, out, keep=2, now=1_000_000)
        await backup_once(db, out, keep=2, now=1_000_100)
        await backup_once(db, out, keep=2, now=1_000_200)
        assert len(list_backups(out)) == 2 and not first.exists()  # oldest was pruned
        import sqlite3
        conn = sqlite3.connect(list_backups(out)[-1])
        assert conn.execute("SELECT points FROM viewer_stats").fetchone()[0] == 42
        conn.close()
        assert prune_backups(out, 0) and list_backups(out) == []

    run(go())
