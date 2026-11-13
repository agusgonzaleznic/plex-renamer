"""Names that went wrong on real downloads: a language code the table lacked,
release tags after the year, a site signature in front of the title, a
category list that swallowed the command, and junk hidden under a name the
system owns. Every title here is made up."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plexrenamer import (classify, cli, db, decisions, executor, lint, scan,  # noqa: E402
                         sidecars, undo, util)
from plexrenamer.planner import Planner  # noqa: E402

RELEASE = "Movies/Lanternfall (2025) [1080p] [WEBRip] [YTS.MX]"
VIDEO = "Lanternfall.2025.1080p.WEBRip-[YTS.MX]"
STEM = "Movies/Lanternfall (2025)/Lanternfall (2025)"


class Titles(unittest.TestCase):
    def info(self, raw, kind="movie"):
        return classify.build_info(kind, raw, [], "Movies")

    def assertTitle(self, raw, title, year, editions=()):
        info = self.info(raw)
        self.assertEqual((info.title, info.year, info.editions), (title, year, list(editions)),
                         raw)

    def test_release_tags_after_the_year_stay_out_of_the_title(self):
        self.assertTitle("Hollow Tide (2026) [RETAIL NOSUBS] [1080p] [WEBRip] [5.1] "
                         "[YTS.GG - YTS.BZ]", "Hollow Tide", 2026)
        self.assertTitle("Hollow.Tide.2026.RETAIL.NOSUBS.1080p.WEBRip.x264.AAC5.1-"
                         "[YTS.GG - YTS.BZ]", "Hollow Tide", 2026)
        self.assertTitle("Hollow_Tide_(2019)_[1080p]", "Hollow Tide", 2019)

    def test_an_edition_after_the_year_is_kept_as_the_edition(self):
        self.assertTitle("Hollow Tide (1982) [Final Cut] [1080p] [YTS.MX]",
                         "Hollow Tide", 1982, ["Final Cut"])
        self.assertTitle("Hollow.Tide.1982.EXTENDED.1080p.BluRay.x264-GRP",
                         "Hollow Tide", 1982, ["Extended"])
        self.assertTitle("Hollow Tide (2019) {imdb-tt0000000} {edition-Final Cut}",
                         "Hollow Tide", 2019, ["Final Cut"])

    def test_an_edition_in_brackets_before_the_year_leaves_no_empty_brackets(self):
        self.assertTitle("Hollow Tide [Extended] (1982) [1080p]", "Hollow Tide", 1982,
                         ["Extended"])

    def test_a_site_signature_in_front_is_dropped(self):
        self.assertTitle("[Example-Site.us] MasterClass - Bob Teaches X",
                         "MasterClass - Bob Teaches X", None)
        self.assertTitle("[ www.example-site.com ] Hollow Tide (2019)", "Hollow Tide", 2019)

    def test_a_title_that_looks_like_release_noise_is_left_alone(self):
        self.assertTitle("[Hex] (2007)", "[Hex]", 2007)                 # brackets, no site
        self.assertTitle("The Example Limited (2007)", "The Example Limited", 2007)
        self.assertTitle("The.Example.Limited.2007.1080p.BluRay.x264-GRP",
                         "The Example Limited", 2007)
        self.assertTitle("Quillhaven 2049 (2017)", "Quillhaven 2049", 2017)
        self.assertTitle("Quillhaven.2049.2017.1080p.BluRay.x264-GRP", "Quillhaven 2049", 2017)
        self.assertTitle("2012 (2009)", "2012", 2009)
        self.assertTitle("2019 - Hollow Tide [1080p]", "Hollow Tide", 2019)

    def test_names_this_tool_wrote_read_back_the_same(self):
        self.assertTitle("Hollow Tide (2019) {imdb-tt0000000}", "Hollow Tide", 2019)
        self.assertEqual(self.info("Hollow Tide (2019) {imdb-tt0000000}").ids,
                         {"imdb": "tt0000000"})
        self.assertTitle("Pebble Ridge (2014) Season 1", "Pebble Ridge", 2014)
        self.assertTitle("Hollow Tide", "Hollow Tide", None)


class EpisodeTitles(unittest.TestCase):
    """An episode title loses only what holds nothing at its ends: separators, a
    bracket without its partner, an empty pair, what is left of a year. A pair
    that holds text stays and the inside is left as it is, so every name the
    tool wrote reads back the same."""

    KEPT = ("Glazing (Cont'd)", "Kilns (Advanced)", "Intro [Live]", "Part (1)",
            "The (Very) Long Title", "(Cont'd) Part Two", "(Bonus) Extra",
            "Kilns ( Part 2 ) Basics", "Glazing () Basics", "Clay [ Wet ] Work", "(1) Intro")
    TRIMMED = (("Title (2019)", "Title"), ("Title [2019]", "Title"), ("Title ()", "Title"),
               ("([2019]) Title", "Title"),
               ("Title (2019", "Title"), ("Episode ([2019])", "Episode"),
               ("Episode [{2019}]", "Episode"), ("(Orphan", "Orphan"), ("Orphan)", "Orphan"),
               ("((Orphan", "Orphan"), ("Orphan))", "Orphan"), ("Title [(", "Title"),
               ("Title ) (", "Title"), (") Title", "Title"), ("Title (a]", "Title (a"),
               ("(Bonus) Extra)", "(Bonus) Extra"), ("(Live (Cont'd)", "Live (Cont'd)"),
               ("(- Intro)", "(Intro)"), ("Remastered (Final 2019)", "Remastered (Final)"))

    def title(self, rest):
        """The title as the planner reads it, which passes the show's title."""
        return classify.parse_episode(f"Gale Teaches Pottery (2016) - S01E03 - {rest}",
                                      "Gale Teaches Pottery")[3]

    @staticmethod
    def plan(root):
        trees = scan.scan(root, ["Classes", "Series"])
        planner = Planner(root)
        return [m for cat in ("Classes", "Series")
                for u in classify.classify_category(cat, trees[cat])
                for m in planner.plan_unit(u).moves]

    def test_a_pair_that_holds_text_stays_and_the_inside_is_left_alone(self):
        for rest in self.KEPT:
            self.assertEqual(self.title(rest), rest)

    def test_what_holds_nothing_goes_from_the_ends(self):
        for rest, title in self.TRIMMED:
            self.assertEqual(self.title(rest), title, rest)
            self.assertEqual(self.title(title), title, f"{rest} does not read back")

    def test_a_bracket_right_after_the_show_name_stays_with_the_title(self):
        for rest, title in (("(] Glazing)", "(] Glazing)"), ("( Part 1 )", "(Part 1)"),
                            ("- ( Kilns )", "(Kilns)")):
            got = classify.parse_episode(f"Kuro - S01E03 - {rest}", "Kuro")[3]
            self.assertEqual(got, title, rest)
            self.assertEqual(classify.parse_episode(f"Kuro - S01E03 - {got}", "Kuro")[3], got)
        self.assertEqual(classify.parse_episode("Kuro + - S01E03 - Glazing", "Kuro")[3], "Glazing")

    def test_a_shouting_title_is_capitalised_after_a_bracket(self):
        self.assertEqual(util.smart_title("(LIVE AT THE ARENA)"), "(Live at the Arena)")
        self.assertEqual(util.smart_title("[LIVE AT THE ARENA]"), "[Live at the Arena]")
        self.assertEqual(util.smart_title("{LIVE AT THE ARENA}"), "{Live at the Arena}")
        self.assertEqual(util.smart_title("EL NIÑO (PARTE 1)"), "El Niño (Parte 1)")
        self.assertEqual(util.smart_title("THE 1ST ONE"), "The 1st One")

    def test_names_planned_from_raw_files_read_back_as_final(self):
        root = tempfile.mkdtemp(prefix="plexrenamer-episodes-")
        state = tempfile.mkdtemp(prefix="plexrenamer-episodes-state-")
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, state, True)
        course, show = "Classes/Gale Teaches Pottery (2016)", "Series/Kuro/Season 1"
        raw = [f"{course}/01 - Introduction.mp4", f"{course}/02 - Glazing (Cont'd).mp4",
               f"{course}/03 - Kilns ( Part 2 ) Basics.mp4", f"{course}/04 - Glazing () Basics.mp4",
               f"{show}/S01E01 - Glazing (Cont'd).mkv", f"{show}/S01E02 (- Intro).mkv",
               f"{show}/S01E03 - (LIVE AT THE ARENA).mkv", f"{show}/S01E04 - Episode ([2019]).mkv"]
        for n, name in enumerate(raw):
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(f"file {n}")
        moves = self.plan(root)
        self.assertEqual(sorted(os.path.relpath(m.src, root) for m in moves), sorted(raw))
        conn = db.connect(os.path.join(state, "j.db"))
        self.addCleanup(conn.close)
        run_id = db.create_run(conn, root, ["Classes", "Series"], "episodes")
        db.insert_ops(conn, run_id, executor.build_ops(
            moves, [os.path.join(root, "Classes"), os.path.join(root, "Series")]))
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        self.assertEqual([os.path.basename(m.dst) for m in self.plan(root)], [])


class LanguageTags(unittest.TestCase):
    def test_a_locale_form_keeps_its_language(self):
        self.assertEqual(util.normalize_lang("fil-PH"), ("fil", False, False, ""))
        self.assertEqual(util.normalize_lang("pt-BR"), ("pt", False, False, ""))
        self.assertEqual(util.normalize_lang("SDH.fil.HI"), ("fil", False, True, ""))


class Library(unittest.TestCase):
    """One release folder with a pack of subtitles, planned for real."""

    SUBS = ("English", "fil", "kan", "Und", "Simplified.chi", "Traditional.chi", "spa")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plexrenamer-names-")
        self.state = tempfile.mkdtemp(prefix="plexrenamer-names-state-")
        folder = os.path.join(self.root, RELEASE)
        os.makedirs(os.path.join(folder, "Subs"))
        names = [f"{VIDEO}.mp4", f"{VIDEO}.srt"] + [f"Subs/{s}.srt" for s in self.SUBS]
        for n, name in enumerate(names):
            with open(os.path.join(folder, name), "w") as fh:
                fh.write(f"file {n}")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def _moves(self):
        trees = scan.scan(self.root, ["Movies"])
        planner = Planner(self.root)
        units = classify.classify_category("Movies", trees["Movies"])
        return [m for u in units for m in planner.plan_unit(u).moves]

    def _planned(self):
        return {os.path.relpath(m.src, os.path.join(self.root, RELEASE)):
                os.path.relpath(m.dst, self.root) for m in self._moves()}

    def test_a_language_code_is_never_filed_under_english(self):
        planned = self._planned()
        self.assertEqual(planned["Subs/fil.srt"], f"{STEM}.fil.srt")       # in the table now
        self.assertEqual(planned["Subs/kan.srt"], f"{STEM}.kan.srt")       # not in the table
        self.assertEqual(planned["Subs/spa.srt"], f"{STEM}.es.srt")
        english = sorted(dst for dst in planned.values()
                         if dst.endswith((".en.srt", ".eng.srt")) or ".en." in dst)
        self.assertEqual(len(english), 3, "the bare one, English and Und, and nothing else")

    def test_an_undetermined_tag_keeps_the_english_guess(self):
        self.assertRegex(self._planned()["Subs/Und.srt"], r"\.(en|eng)\.srt$|\.en\.srt$")
        self.assertNotIn(".und.", self._planned()["Subs/Und.srt"])

    def test_two_files_of_one_language_take_the_two_documented_code_widths(self):
        # deliberate: Plex documents both widths and no label, so this is not a bug
        planned = self._planned()
        self.assertEqual(planned[f"{VIDEO}.srt"], f"{STEM}.en.srt")
        self.assertEqual(planned["Subs/English.srt"], f"{STEM}.eng.srt")
        self.assertEqual({planned["Subs/Simplified.chi.srt"], planned["Subs/Traditional.chi.srt"]},
                         {f"{STEM}.zh.srt", f"{STEM}.chi.srt"})

    def test_lint_names_a_code_the_table_does_not_know(self):
        issues = lint.check(self._moves(), self.root, ["Movies"])
        unknown = issues.get("subtitle language code this tool does not know", [])
        self.assertEqual(unknown, [f"{STEM}.kan.srt"])

    def test_a_second_pass_changes_nothing(self):
        moves = self._moves()
        self.assertEqual(executor.validate(moves), [])
        ops = executor.build_ops(moves, [os.path.join(self.root, "Movies")])
        conn = db.connect(os.path.join(self.state, "j.db"))
        run_id = db.create_run(conn, self.root, ["Movies"], "names")
        db.insert_ops(conn, run_id, ops)
        _ok, failed = executor.apply_ops(conn, run_id, quiet=True)
        self.assertEqual(failed, 0)
        self.assertEqual(self._moves(), [], "names the tool wrote must read back as final")


class HiddenNames(unittest.TestCase):
    """A dot hides a file from the scanner, but never by giving it a name the
    system owns: Finder rewrites .DS_Store, macOS removes "._" files with the
    file or folder of the rest of their name, and file servers often hide both
    and delete them with their folder."""

    JUNK = ("DS Store", "DS_Store", "_YTS.MX - Official site.jpg", "_read me.txt",
            "hidden-_read me.txt")
    HIDDEN_UNIT = "Movies/_Bonus Disc"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plexrenamer-hidden-")
        self.state = tempfile.mkdtemp(prefix="plexrenamer-hidden-state-")
        names = [f"{RELEASE}/{VIDEO}.mp4"] + [f"{RELEASE}/{j}" for j in self.JUNK] + \
            [f"{self.HIDDEN_UNIT}/Bonus.mkv"]
        for n, name in enumerate(names):
            path = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(f"file {n}")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def _moves(self):
        trees = scan.scan(self.root, ["Movies"])
        units = classify.classify_category("Movies", trees["Movies"])
        units, _, _ = decisions.apply_to(units, {self.HIDDEN_UNIT: {"hide": True}})
        planner = Planner(self.root)
        return [m for u in units for m in planner.plan_unit(u).moves]

    def _files(self):
        out = {}
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                with open(os.path.join(base, name)) as fh:
                    out[os.path.relpath(os.path.join(base, name), self.root)] = fh.read()
        return out

    def test_the_names_the_system_owns(self):
        for name in (".DS_Store", ".ds_store", "._Movie.mkv", "._", "._a\nb"):
            self.assertTrue(sidecars.SYSTEM_NAME_RE.fullmatch(name), name)
        for name in (".DS Store", ".DS_Store-1", ".hidden-DS_Store", ".read me.txt", "_x"):
            self.assertFalse(sidecars.SYSTEM_NAME_RE.fullmatch(name), name)

    def test_a_dot_goes_in_front_unless_it_gives_such_a_name(self):
        self.assertEqual(sidecars._hidden("read me.txt"), ".read me.txt")
        self.assertEqual(sidecars._hidden("DS Store"), ".DS Store")
        self.assertEqual(sidecars._hidden(".already hidden"), ".already hidden")
        self.assertEqual(sidecars._hidden("DS_Store"), ".hidden-DS_Store")
        self.assertEqual(sidecars._hidden("ds_store"), ".hidden-ds_store")
        self.assertEqual(sidecars._hidden("_read me.txt"), ".hidden-_read me.txt")
        self.assertEqual(sidecars._hidden("_a\nb"), ".hidden-_a\nb")

    def test_two_names_never_share_a_hidden_name(self):
        names = ("_x", "hıdden-_x", "hidden-hidden-_x", "x", "hidden-x",
                 "DS_Store", "hidden-DS_Store", "DS Store")
        for fold in (str.lower, str.upper):     # as a share may compare them
            hidden = [fold(sidecars._hidden(n)) for n in names]
            self.assertEqual(len(set(hidden)), len(names), hidden)
        self.assertEqual(sidecars._hidden("hidden-_x"), ".hidden-hidden-_x")
        self.assertEqual(sidecars._hidden("Hidden-_x"), ".hidden-Hidden-_x")

    def test_no_planned_path_takes_a_name_the_system_owns(self):
        moves = self._moves()
        self.assertEqual(executor.validate(moves), [])
        planned = {os.path.basename(m.src): os.path.relpath(m.dst, self.root) for m in moves}
        new = "Movies/Lanternfall (2025)"
        self.assertEqual(planned["DS Store"], f"{new}/.DS Store")
        self.assertEqual(planned["DS_Store"], f"{new}/.hidden-DS_Store")
        self.assertEqual(planned["_YTS.MX - Official site.jpg"],
                         f"{new}/.hidden-_YTS.MX - Official site.jpg")
        self.assertEqual(planned["_read me.txt"], f"{new}/.hidden-_read me.txt")
        self.assertEqual(planned["hidden-_read me.txt"], f"{new}/.hidden-hidden-_read me.txt")
        self.assertEqual(planned["Bonus.mkv"], "Movies/.hidden-_Bonus Disc/Bonus.mkv")
        for dst in planned.values():
            for part in dst.split(os.sep):
                self.assertFalse(sidecars.SYSTEM_NAME_RE.fullmatch(part), dst)

    def test_hidden_names_read_back_as_final_and_undo_restores_them(self):
        before = self._files()
        conn = db.connect(os.path.join(self.state, "j.db"))
        self.addCleanup(conn.close)
        run_id = db.create_run(conn, self.root, ["Movies"], "hidden")
        db.insert_ops(conn, run_id, executor.build_ops(
            self._moves(), [os.path.join(self.root, "Movies")]))
        self.assertEqual(executor.apply_ops(conn, run_id, quiet=True)[1], 0)
        after = self._files()
        self.assertEqual(sorted(after.values()), sorted(before.values()))
        self.assertEqual(sum(os.path.basename(p).startswith(".") for p in after), 5)
        self.assertEqual(self._moves(), [], "a hidden name must read back as final")
        self.assertEqual(undo.undo_run(conn, run_id, quiet=True)[1], 0)
        self.assertEqual(self._files(), before)


class CommandLine(unittest.TestCase):
    def parse(self, *argv):
        seen = {}

        def capture(args):
            seen.update(cmd=args.cmd, categories=args.categories)
            return 0
        with mock.patch.object(cli, "cmd_status", capture), \
                mock.patch.object(cli, "cmd_plan", capture):
            self.assertEqual(cli.main(list(argv)), 0)
        return seen["cmd"], seen["categories"]

    def test_the_category_list_ends_at_the_command(self):
        self.assertEqual(self.parse("--categories", "Movies", "Series", "status"),
                         ("status", ["Movies", "Series"]))
        self.assertEqual(self.parse("--categories", "Animated Movies", "plan", "--tag", "x"),
                         ("plan", ["Animated Movies"]))

    def test_the_category_list_still_ends_at_an_option(self):
        self.assertEqual(self.parse("--categories", "Movies", "Series", "--db", "x.db", "status"),
                         ("status", ["Movies", "Series"]))
        self.assertEqual(self.parse("--categories=Movies", "status"), ("status", ["Movies"]))

    def test_without_the_option_every_category_is_used(self):
        cmd, categories = self.parse("status")
        self.assertEqual((cmd, categories), ("status", cli.CATEGORIES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
