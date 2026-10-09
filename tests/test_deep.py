import itertools
import json

from creature import deep


def test_a_round_changes_only_the_files_it_names_and_edits_apply_once():
    space = deep.Workspace({"frame.py": "A = 1\nB = 2\n", "layout.json": "{}"})
    problems = deep.apply(
        space, {"files": [], "edits": [{"path": "frame.py", "find": "B = 2", "replace": "B = 3"}]}
    )
    assert problems == [] and space.files == {"frame.py": "A = 1\nB = 3\n", "layout.json": "{}"}
    twice = deep.Workspace({"frame.py": "x\nx\n"})
    assert "2 times" in deep.apply(twice, {"edits": [{"path": "frame.py", "find": "x", "replace": "y"}]})[0]
    assert twice.files == {"frame.py": "x\nx\n"}


def test_bad_names_bad_json_and_too_many_files_change_nothing():
    space = deep.Workspace({"frame.py": "pass\n"})
    assert deep.apply(space, {"files": [{"path": "../evil.py", "content": "x"}]})
    assert deep.apply(space, {"files": [{"path": "layout.json", "content": "{nope"}]})
    many = [{"path": f"m{i}.py", "content": "x"} for i in range(deep.MAX_FILES)]
    assert deep.apply(space, {"files": many})
    assert space.files == {"frame.py": "pass\n"}


def test_the_installed_skill_carries_its_files_inside_fixed_wrapper_code():
    files = {"frame.py": "def frame(n, ctx):\n    return None\n", "layout.json": json.dumps({"frames": 30})}
    code = deep.skill_code(files)
    assert code.startswith(deep.WRAPPER.split("__FILES__")[0]) and repr(dict(sorted(files.items()))) in code
    assert "def run(input, work)" in code


def test_the_round_prompt_never_carries_held_out_criteria():
    from creature import criteria
    from creature.criteria import Spec

    out = criteria.clip_format(3)
    spec = Spec("e", "s-x", "try", "", "t", "Hi", {}, out, ("seen one",), ("SECRET held out",), ())
    text = deep.prompt(spec, deep.Workspace(), "feedback", "", 1, 30)
    assert "seen one" in text and "SECRET" not in text


# --- the round loop: best version kept, budget ends it, bad answers and failed clips feed back -----

from creature import criteria  # noqa: E402
from creature.criteria import Spec  # noqa: E402
from creature.ledger import Ledger  # noqa: E402
from creature.llm import Model, ModelError  # noqa: E402
from creature.perceive import Reel  # noqa: E402
from tests.fakes import FakeModel  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"
V1 = {"path": "frame.py", "content": "V = 1\n"}
LAYOUT = {"path": "layout.json", "content": json.dumps({"frames": 90})}


def _reply(note, files=(), edits=(), done=False):
    return {"note": note, "files": list(files), "edits": list(edits), "done": done}


def _edit(find, replace):
    return {"path": "frame.py", "find": find, "replace": replace}


class Rounds:
    """deep.build with the preview faked (scores scripted) and finish recorded."""

    def __init__(self, home, tmp_path, monkeypatch, scores, finish_ok=(True,), budget=4.0):
        self.fake = FakeModel()
        self.ledger = Ledger.start(home, run_id="deep")
        self.model = Model(
            self.fake, self.ledger, budget_usd=budget, reserve_usd=0.25, model="claude-haiku-5-5"
        )
        strip = tmp_path / "reel-strip.png"
        strip.write_bytes(PNG)
        self.reel = Reel(
            "reel.mp4", "", "", "", tmp_path / "reel.mp4", strip, (0.5, 1.5), 12.0, 1080, 1920, 30.0
        )
        pairs = tmp_path / "pairs.png"
        pairs.write_bytes(PNG)
        scores = iter(scores)
        shown = (("pairs: reel left, yours right", pairs),)
        monkeypatch.setattr(deep, "_preview", lambda *a, **k: deep.Preview(shown, None, next(scores), "d"))
        out = criteria.clip_format(3)
        self.spec = Spec("e", "s-x", "try", "", "t", "Hi", {}, out, ("seen one",), ("SECRET held out",), ())
        self.finished = []
        oks = iter(finish_ok)

        def finish(code, number):
            self.finished.append((number, code))
            ok = next(oks)
            return deep.Outcome(ok, "" if ok else "Judge, not met: seen one (seen: nothing typed)", {})

        self.finish = finish
        self.folder = tmp_path / "rounds"

    def build(self, rounds=5, assets=None, limits=None):
        return deep.build(
            self.model, self.spec, self.ledger, reel=self.reel, rounds=rounds, cap_usd=0.25,
            model_name="claude-opus-5-5", limits=limits, image="img", folder=self.folder, finish=self.finish,
            assets=assets,
        )  # fmt: skip

    def prompts(self):
        return [c.prompt for c in self.fake.calls]

    def events(self, kind):
        from creature import ledger

        return [e for e in ledger.read(self.ledger.path) if e["type"] == kind]


def test_a_worse_round_goes_back_to_the_best_files_and_done_renders_the_best(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 30.0, 10.0])
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")]))  # scores worse: reverted
    r.fake.queue("forge", _reply("v3", edits=[_edit("V = 1", "V = 3")], done=True))  # applies to v1
    result = r.build()
    assert result.ok and result.rounds == 3 and result.files["frame.py"] == "V = 3\n"
    [(number, code)] = r.finished
    assert number == 3 and code == deep.skill_code(result.files)
    first, second, third = r.prompts()
    assert "No files yet" in first and "your best so far" in second
    assert "worse than round 1" in third and "back to round 1's files" in third
    assert "V = 1\n" in third and "V = 2" not in third  # the forge sees the files it really has
    assert [e["best_round"] for e in r.events("round_score")] == [1, 1, 3]
    labels = [[label for label, _ in c.images] for c in r.fake.calls]
    assert labels[0] == ["reel frames"] and labels[1] == labels[2] == ["pairs: reel left, yours right"]


def test_the_budget_ends_the_rounds_and_nothing_is_finished(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 15.0], budget=4.0)
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")]), cost_usd=3.9)  # the run's money
    result = r.build(rounds=30)
    assert not result.ok and result.rounds == 2 and result.gap.startswith("budget:")
    assert r.finished == [] and result.files["frame.py"] == "V = 2\n"
    assert r.events("model_refused") and len(r.events("round")) == 2


def test_a_malformed_answer_costs_a_round_and_the_next_round_is_told(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0])
    r.fake.queue("forge", ModelError("cut off"))
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT], done=True))
    result = r.build()
    assert result.ok and result.rounds == 2
    assert "could not be used" in r.prompts()[1]
    [bad, good] = r.events("round")
    assert bad["ok"] is False and "cut off" in bad["error"] and good["ok"] is True


def test_a_clip_that_fails_the_judge_feeds_the_next_round_without_the_hidden_criterion(
    home, tmp_path, monkeypatch
):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 10.0], finish_ok=(False, True))
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT], done=True))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")], done=True))
    result = r.build()
    assert result.ok and result.rounds == 2 and [n for n, _ in r.finished] == [1, 2]
    second = r.prompts()[1]
    assert "Your whole clip was checked and did not pass" in second and "Judge, not met: seen one" in second
    assert "SECRET" not in second
    assert "SECRET" not in deep.SYSTEM


# --- the library is picked by the material the reel is made of --------------------------------------


def _item(kind, name, *tags, ext="png"):
    return {"jmeno": name, "kategorie": kind, "stitky": list(tags), "soubor": f"{kind}/{name}.{ext}"}


MATERIAL = [
    _item("textury", "zrno-jemne", "zrno", "grain", "film"),
    _item("textury", "papir-vlakna", "papir", "paper", "vlakna"),
    _item("textury", "sum-bily", "sum", "noise", "white noise"),
    _item("textury", "poloton-tecky", "poloton", "halftone"),
    _item("textury", "kuze", "poly haven", "fotografie", "fabric", "leather", ext="jpg"),
    _item("zare", "zare-mekka", "zare", "glow", "svetlo"),
    _item("fotky", "priroda-waterfall", "fotografie", "priroda", "waterfall", ext="jpg"),
    _item("fotky", "architektura-cathedral", "fotografie", "architektura", "cathedral", ext="jpg"),
    _item("ikony", "zvonek", "bell", "zvonek", ext="svg"),
    _item("ikony", "domov", "home", "domov", ext="svg"),
    _item("postavy", "jde", "open peeps", "postava", "chuze", ext="svg"),
    _item("pisma", "inter", "grotesk", "sans", "text", ext="ttf"),
    _item("pisma", "serif", "serif", "patkové", "kurzíva", ext="ttf"),
    _item("pisma", "mono", "mono", "kód", ext="ttf"),
    _item("tvary", "kruh", "kruh", "circle", "plny", ext="svg"),
    _item("sipky", "sipka", "sipka", "arrow", ext="svg"),
    _item("zvuk", "sum-tlumeny", "sum", "noise", "uder", ext="wav"),
]


def _library(tmp_path, items=MATERIAL):
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text("Where things are: textures in textury/.\n")
    (root / "manifest.json").write_text(json.dumps({"prvky": items}, ensure_ascii=False))
    return str(root)


def _material_spec(*said, text="", effect="Effect", task="Do it.", params=None, held_out=(), **more):
    out = criteria.clip_format(3)
    return Spec(effect, "s-x", "try", "", task, text, params or {}, out, tuple(said), held_out, (), **more)


def _picked(tmp_path, spec, items=MATERIAL):
    return [i["soubor"] for i in deep.stock(_library(tmp_path, items), spec)[1]]


def test_the_picked_items_follow_the_words_for_a_material_and_the_closest_tags_come_first(tmp_path):
    grain = _picked(tmp_path, _material_spec("Film grain flickers over the frame."))
    assert grain == ["textury/zrno-jemne.png", "textury/sum-bily.png"]  # noise is grain; the sound is not
    paper = _picked(tmp_path, _material_spec("The title sits on a paper texture."))
    assert paper[0] == "textury/papir-vlakna.png" and {p.split("/")[0] for p in paper} == {"textury"}
    assert len(paper) == 5  # "texture" is every texture the library has


def test_each_material_word_picks_its_own_kind_of_item(tmp_path):
    wanted = {
        "A photo of a cathedral zooms.": ["fotky/architektura-cathedral.jpg", "fotky/priroda-waterfall.jpg"],
        "A bell icon rings.": ["ikony/zvonek.svg", "ikony/domov.svg"],
        "A silhouette of a person walks.": ["postavy/jde.svg"],
        "A soft glow behind the title.": ["zare/zare-mekka.png"],
        "Soft leather and denim.": ["textury/kuze.jpg"],
        "Halftone dots fade.": ["textury/poloton-tecky.png"],
    }  # fmt: skip
    for said, items in wanted.items():
        assert _picked(tmp_path, _material_spec(said)) == items, said


def test_nothing_named_picks_nothing_and_the_shelf_says_so(tmp_path):
    spec = _material_spec("A circle grows, an arrow points at it, a thud lands on the beat.")
    assert _picked(tmp_path, spec) == []
    shelf = deep.stock(_library(tmp_path), spec)[0]
    assert "Where things are" in shelf and "(none: the criteria name no texture" in shelf


def test_shapes_arrows_frames_and_sounds_are_not_material_even_when_their_tags_match(tmp_path):
    geometry = [
        _item("tvary", "glow-kruh", "circle", "glow", "grain", ext="svg"),
        _item("sipky", "zrnita-sipka", "arrow", "grain", ext="svg"),
        _item("ramecky-a-rozhrani", "ram", "grain", "glow", ext="svg"),
        _item("zvuk", "svih", "noise", "whoosh", ext="wav"),
    ]
    picked = _picked(tmp_path, _material_spec("Film grain and a soft glow."), items=MATERIAL + geometry)
    assert picked and not {p.split("/")[0] for p in picked} & {"tvary", "sipky", "ramecky-a-rozhrani", "zvuk"}


def test_the_effects_name_the_text_the_params_and_the_held_out_criteria_pick_nothing(tmp_path):
    spec = _material_spec(
        "The title scales up.", effect="Photo icon silhouette serif glow", params={"font": "serif photo"},
        held_out=("A paper texture with grain and a bell icon.",), text="",
    )  # fmt: skip
    assert _picked(tmp_path, spec) == []


def test_gaps_and_a_montages_surfaces_say_what_the_reel_shows_too(tmp_path):
    gap = {"what": "product photos", "needs": "photos the user supplies"}
    assert set(_picked(tmp_path, _material_spec("Cuts every second.", gaps=(gap,)))) == {
        "fotky/architektura-cathedral.jpg", "fotky/priroda-waterfall.jpg",
    }  # fmt: skip
    surface = _material_spec("A bell icon rings.", task="Cards with a halftone pattern.")
    montage = _material_spec("Cuts every second.", parts=(surface,))
    assert _picked(tmp_path, montage) == ["textury/poloton-tecky.png", "ikony/zvonek.svg", "ikony/domov.svg"]


def test_a_clip_with_text_gets_fonts_and_type_words_rank_them(tmp_path):
    assert not [p for p in _picked(tmp_path, _material_spec("The ring grows.")) if p.startswith("pisma/")]
    plain = _picked(tmp_path, _material_spec("The ring grows.", text="Hi"))
    assert sorted(plain) == ["pisma/inter.ttf", "pisma/mono.ttf", "pisma/serif.ttf"]
    assert _picked(tmp_path, _material_spec("A serif title.", text="Hi"))[0] == "pisma/serif.ttf"
    assert _picked(tmp_path, _material_spec("Set in a monospaced face.", text="Hi"))[0] == "pisma/mono.ttf"
    # "sans-serif" is the other kind: the word serif inside it picks nothing
    assert _picked(tmp_path, _material_spec("A sans-serif title.")) == ["pisma/inter.ttf"]


def test_kinds_take_turns_so_many_icons_cannot_crowd_out_the_photos_and_the_cap_holds(tmp_path):
    icons = [_item("ikony", f"i{n}", "icon", ext="svg") for n in range(40)]
    photos = [_item("fotky", f"p{n}", "fotografie", ext="jpg") for n in range(10)]
    picked = _picked(tmp_path, _material_spec("A photo and an icon."), items=icons + photos)
    kinds = [p.split("/")[0] for p in picked]
    assert len(picked) == deep.LIBRARY_SHOWN and kinds.count("fotky") == 10
    assert kinds[:4] == ["ikony", "fotky", "ikony", "fotky"]
    fonts = [_item("pisma", f"f{n}", "sans", ext="ttf") for n in range(20)]
    shown = _picked(tmp_path, _material_spec("A font.", text="Hi"), items=fonts)
    assert len(shown) == deep.FONTS_SHOWN


def test_the_picks_are_the_same_every_time(tmp_path):
    spec = _material_spec("A photo, an icon and film grain on paper.", text="Hi")
    first = _picked(tmp_path, spec)
    assert first and first == _picked(tmp_path, spec) == _picked(tmp_path, spec, items=list(MATERIAL))


def test_the_forge_is_told_where_textures_come_from_and_where_geometry_does():
    said = " ".join(deep.SYSTEM.split())
    sentence = (
        "When an asset library is mounted at /assets (described in the task), textures, photos, icons and "
        "fonts come from it, and geometry is drawn by the code itself."
    )
    assert said.count(sentence) == 1 and "SECRET" not in deep.SYSTEM


# --- round 1 also shows the library items as a contact sheet ----------------------------------------

LIMITS = deep.workshop.Limits(120, 2048, 2, 256, 512, 64, 200, assets="/elsewhere")


class Shop:
    """workshop.run faked: records what the sheet code was given and answers with a scripted result."""

    def __init__(self, monkeypatch, value=None, outputs=None, ok=True, error=None):
        self.calls = []
        self.answer = deep.workshop.WorkshopResult(ok, value, outputs or {}, error, None, 0.1)
        monkeypatch.setattr(deep.workshop, "run", self.run)

    def run(self, code, input, files=None, *, limits, image):
        self.calls.append((code, input, limits, image))
        return self.answer


SHEETS = itertools.count()


def _sheet(tmp_path, home, chosen, shop_limits=LIMITS):
    ledger = Ledger.start(home, run_id=f"sheet-{next(SHEETS)}")
    shown = deep.library_sheet(chosen, "/lib", shop_limits, "img", tmp_path / "rounds", ledger)
    return shown, ledger


def _events(ledger, kind):
    from creature import ledger as ledger_module

    return [e for e in ledger_module.read(ledger.path) if e["type"] == kind]


def test_the_sheet_is_made_in_the_workshop_from_the_picked_items_that_can_be_shown(
    home, tmp_path, monkeypatch
):
    shop = Shop(monkeypatch, {"file": "library.png", "tiles": 3}, {"library.png": PNG})
    chosen = [
        _item("textury", "zrno", ext="png"), _item("ikony", "zvonek", ext="svg"),
        _item("fotky", "foto", ext="jpg"), _item("pisma", "inter", ext="ttf"),
        _item("zvuk", "klik", ext="wav"), {"soubor": "../../etc/x.png"}, {"soubor": "/etc/x.png"},
        {"soubor": "textury/./a.png"}, {"soubor": "a//b.png"},
    ]  # fmt: skip
    [(label, path)], ledger = _sheet(tmp_path, home, chosen)
    [(code, given, limits, image)] = shop.calls
    assert code == deep.SHEET_CODE and image == "img"
    assert given["items"] == [
        {"path": "/assets/textury/zrno.png", "label": "zrno"},
        {"path": "/assets/ikony/zvonek.svg", "label": "zvonek"},
        {"path": "/assets/fotky/foto.jpg", "label": "foto"},
    ]  # no font, no sound, no path that walks
    assert given["max_bytes"] == deep.MAX_IMAGE_BYTES
    assert limits.assets == "/lib" and limits.timeout_s == deep.SHEET_TIMEOUT_S  # the folder it picked from
    assert "library items" in label and path.read_bytes() == PNG and path.parent == tmp_path / "rounds"
    [event] = _events(ledger, "library_sheet")
    assert event["ok"] is True and event["items"] == 3


def test_the_sheet_holds_at_most_a_set_number_of_tiles(home, tmp_path, monkeypatch):
    shop = Shop(monkeypatch, {"file": "library.png"}, {"library.png": PNG})
    many = [_item("ikony", f"i{n}", ext="svg") for n in range(deep.SHEET_TILES + 10)]
    _sheet(tmp_path, home, many)
    assert len(shop.calls[0][1]["items"]) == deep.SHEET_TILES


def test_no_sheet_when_nothing_picked_can_be_shown_or_the_workshop_is_not_there(home, tmp_path, monkeypatch):
    shop = Shop(monkeypatch, {"file": "library.png"}, {"library.png": PNG})
    only_type = [_item("pisma", "inter", ext="ttf"), _item("zvuk", "klik", ext="wav")]
    assert _sheet(tmp_path, home, only_type)[0] == () and _sheet(tmp_path, home, [])[0] == ()
    assert _sheet(tmp_path, home, [_item("fotky", "foto", ext="jpg")], shop_limits=None)[0] == ()
    assert shop.calls == [] and not (tmp_path / "rounds").exists()


def test_a_failed_or_oversized_sheet_is_no_sheet_and_the_ledger_says_why(home, tmp_path, monkeypatch):
    chosen = [_item("fotky", "foto", ext="jpg")]
    Shop(monkeypatch, ok=False, error="killed: timeout")
    shown, ledger = _sheet(tmp_path, home, chosen)
    assert shown == () and "timeout" in _events(ledger, "library_sheet")[0]["error"]
    Shop(monkeypatch, {"tiles": 0, "skipped": [["/assets/fotky/foto.jpg", "UnidentifiedImageError"]]})
    assert _sheet(tmp_path, home, chosen)[0] == ()
    big = b"x" * (deep.MAX_IMAGE_BYTES + 1)  # over llm.MAX_IMAGE_BYTES: the model call would refuse it
    Shop(monkeypatch, {"file": "library.png"}, {"library.png": big})
    shown, ledger = _sheet(tmp_path, home, chosen)
    assert shown == () and not list((tmp_path / "rounds").glob("*")) and _events(ledger, "library_sheet")


def test_the_first_round_sees_the_sheet_and_the_later_rounds_see_the_pairs(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 10.0])
    r.spec = _material_spec("A photo of a cathedral.", text="Hi")
    Shop(monkeypatch, {"file": "library.png"}, {"library.png": PNG})
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")], done=True))
    result = r.build(assets=_library(tmp_path), limits=LIMITS)
    assert result.ok
    labels = [[label for label, _ in c.images] for c in r.fake.calls]
    assert labels[0] == ["reel frames", deep.SHEET_LABEL]
    assert labels[1] == ["pairs: reel left, yours right"]
    assert "/assets/fotky/architektura-cathedral.jpg" in r.prompts()[0]  # the same items are in the text
    assert (r.fake.calls[0].images[1][1]).read_bytes() == PNG


def test_without_picked_items_or_a_library_the_first_round_is_only_the_reel(home, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the workshop must not be asked for a sheet")

    monkeypatch.setattr(deep.workshop, "run", boom)
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0])
    r.spec = _material_spec("The ring grows.")
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT], done=True))
    r.build(assets=_library(tmp_path), limits=LIMITS)  # the criteria name no material: nothing picked
    assert [label for label, _ in r.fake.calls[0].images] == ["reel frames"]
    assert "(none: the criteria name no texture" in r.prompts()[0]


def test_a_plateau_checks_the_best_version_once_and_stops(home, tmp_path, monkeypatch):
    # 9. 10. 2026: a run paid 24 Opus rounds ($3.6) long after its score had stopped improving
    r = Rounds(home, tmp_path, monkeypatch, scores=[10.0] + [9.9] * 8, finish_ok=(False,))
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    for n in range(2, 8):
        r.fake.queue("forge", _reply(f"v{n}", edits=[_edit("V = 1", f"V = {n}")]))
    result = r.build(rounds=30)
    assert not result.ok and result.gap.startswith("plateau:") and "seen one" in result.gap
    assert result.rounds == 7 and len(r.fake.calls) == 7  # rounds 2..7 bought less than 3 %: stop
    [(number, _)] = r.finished
    assert number == 7 and r.events("plateau")[0]["best_round"] in range(1, 8)


def test_a_score_that_keeps_improving_is_not_a_plateau(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[40.0 * 0.9**n for n in range(9)])
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    for n in range(2, 9):
        r.fake.queue("forge", _reply(f"v{n}", edits=[_edit(f"V = {n - 1}" if n > 2 else "V = 1", f"V = {n}")],
                                     done=n == 8))  # fmt: skip
    result = r.build(rounds=30)
    assert result.ok and result.rounds == 8 and r.events("plateau") == []


def test_the_forge_is_told_that_motion_eases():
    # 9. 10.: "ok, but it could be smoother" on a montage the judge passed: easing is a rule, not a recipe
    assert "eases in and out" in deep.SYSTEM and "at least 6 frames" in deep.SYSTEM
