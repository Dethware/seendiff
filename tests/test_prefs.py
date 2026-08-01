from seendiff import store


def test_prefs_roundtrip_and_upsert(tmp_path):
    db = store.open_db(str(tmp_path / "t.db"))
    assert store.get_prefs(db) == {}
    store.set_pref(db, "seendiff.theme", "space")
    store.set_pref(db, "seendiff.font", "Ubuntu Sans Mono")
    store.set_pref(db, "seendiff.theme", "hailmary")
    assert store.get_prefs(db) == {
        "seendiff.theme": "hailmary",
        "seendiff.font": "Ubuntu Sans Mono",
    }


def test_prefs_survive_prune(tmp_path):
    db = store.open_db(str(tmp_path / "t.db"))
    store.set_pref(db, "seendiff.theme", "dark")
    store.prune(db, days=0)
    assert store.get_prefs(db)["seendiff.theme"] == "dark"
