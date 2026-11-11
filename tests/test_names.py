"""Names that went wrong on real downloads: a language code the table lacked,
release tags after the year, a site signature in front of the title, and a
category list that swallowed the command. Every title here is made up."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plexrenamer import classify, cli, db, executor, lint, scan, util  # noqa: E402
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
