"""Public synthetic fixtures: inventory, documentation and arbitrary strings."""

import random
import unittest
from html.parser import HTMLParser

from telegram_formatting import html_chunks, html_to_plain, md_to_html


class _BalanceChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"b", "i", "u", "s", "code", "pre", "a"}:
            raise AssertionError("Unsupported tag: " + tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise AssertionError("Unbalanced tag: " + tag)


class MarkdownTests(unittest.TestCase):
    def test_existing_supported_constructs_and_identifiers(self):
        source = (
            "## Synthetic heading ##\r\n"
            "**bold**; *italic*; ~~removed~~; strain_yesterday; __literal__\r"
            "- first\r\n  + nested\n* second\n---\n"
            "`a_b < c & **literal**`"
        )
        self.assertEqual(md_to_html(source), (
            "<b>Synthetic heading</b>\n"
            "<b>bold</b>; <i>italic</i>; <s>removed</s>; "
            "strain_yesterday; __literal__\n"
            "\u2022 first\n  \u2022 nested\n\u2022 second\n"
            "<code>a_b &lt; c &amp; **literal**</code>"
        ))

    def test_empty_input(self):
        self.assertEqual(md_to_html(""), "")
        self.assertEqual(html_to_plain(""), "")
        self.assertEqual(html_chunks(""), [""])

    def test_raw_html_is_visible_text_not_executable_markup(self):
        source = '<b>untrusted</b> <script>alert("x")</script> & data'
        rendered = md_to_html(source)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<b>", rendered)
        self.assertEqual(html_to_plain(rendered), source)

    def test_private_use_characters_do_not_collide_with_placeholders(self):
        source = "\ue0000\ue001 `sample` \ue000999\ue001"
        self.assertEqual(
            html_to_plain(md_to_html(source)), "\ue0000\ue001 sample \ue000999\ue001")

    def test_http_links_escape_destination_once(self):
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                url = scheme + "://example.test/docs?q=one&next=two&not=three"
                rendered = md_to_html("[**Docs & notes**](" + url + ")")
                self.assertIn("q=one&amp;next=two&amp;not=three", rendered)
                self.assertNotIn("&amp;amp;", rendered)
                parser = _BalanceChecker()
                parser.feed(rendered)
                self.assertEqual(parser.links, [url])
                self.assertEqual(html_to_plain(rendered), "Docs & notes")

    def test_url_attributes_are_escaped_and_parentheses_preserved(self):
        url = 'https://example.test/page_(v2)?q="x"&k=<y>'
        rendered = md_to_html("[Reference](" + url + ")")
        self.assertIn("&quot;x&quot;", rendered)
        self.assertIn("&lt;y&gt;", rendered)
        parser = _BalanceChecker()
        parser.feed(rendered)
        self.assertEqual(parser.links, [url])

    def test_non_http_links_and_underscore_emphasis_remain_literal(self):
        source = "[unsafe](javascript:alert) _plain_ item_id x*y*z"
        self.assertEqual(md_to_html(source), source)

    def test_multiline_bold_and_nested_emphasis(self):
        self.assertEqual(
            md_to_html("**line one\nline *two* and ~~three~~** ***both***"),
            "<b>line one\nline <i>two</i> and <s>three</s></b> <b><i>both</i></b>")

    def test_code_is_protected_from_formatting_and_raw_html(self):
        self.assertEqual(
            md_to_html("Use ``a|`b` & **c**`` then `x<y`."),
            "Use <code>a|`b` &amp; **c**</code> then <code>x&lt;y</code>.")
        self.assertEqual(
            md_to_html("Before\n```text\n**literal**\n<a>& x_y\n```\nAfter"),
            "Before\n<pre>**literal**\n&lt;a&gt;&amp; x_y</pre>\nAfter")

    def test_code_does_not_overlap_emphasis_or_links(self):
        self.assertEqual(
            md_to_html("**bold `code` end**"),
            "<b>bold </b><code>code</code><b> end</b>")
        self.assertEqual(
            md_to_html("[`sample`](https://example.test)"),
            '<a href="https://example.test">sample</a>')

    def test_whole_message_fence_is_unwrapped_for_compatibility(self):
        self.assertEqual(
            md_to_html("```markdown\n# Heading\n**Synthetic**\n```"),
            "<b>Heading</b>\n<b>Synthetic</b>")

    def test_standard_table_becomes_labeled_cards_with_total(self):
        rendered = md_to_html(
            "Inventory\n\n"
            "| Item | Count | Status |\n"
            "| :--- | ---: | :---: |\n"
            "| Widget A | 3 | Ready |\n"
            "| Widget B | 5 | Pending |\n"
            "| **Total** | **8** | Reviewed |\n\nDone."
        )
        self.assertEqual(rendered, (
            "Inventory\n\n"
            "\u2022 <b>Item:</b> Widget A\n\u2022 <b>Count:</b> 3\n"
            "\u2022 <b>Status:</b> Ready\n\n"
            "\u2022 <b>Item:</b> Widget B\n\u2022 <b>Count:</b> 5\n"
            "\u2022 <b>Status:</b> Pending\n\n"
            "\u2022 <b>Item:</b> <b>Total</b>\n\u2022 <b>Count:</b> <b>8</b>\n"
            "\u2022 <b>Status:</b> Reviewed\n\nDone."
        ))
        self.assertNotIn("|", rendered)
        self.assertNotIn("---", rendered)

    def test_borderless_tables_and_multiple_tables(self):
        rendered = md_to_html(
            "Name | Count\n--- | ---\nAlpha | 1\n\n"
            "Kind | Value\n--- | ---\nBeta | 2"
        )
        self.assertEqual(html_to_plain(rendered),
                         "\u2022 Name: Alpha\n\u2022 Count: 1\n\n"
                         "\u2022 Kind: Beta\n\u2022 Value: 2")

    def test_escaped_and_code_pipes_are_not_cell_boundaries(self):
        rendered = md_to_html(
            r"| Name \| alias | `field|key` | Notes |" "\n"
            "| --- | --- | --- |\n"
            r"| A \| B | `left|right` | ``x|`y`|z`` |"
        )
        self.assertEqual(html_to_plain(rendered),
                         "\u2022 Name | alias: A | B\n"
                         "\u2022 field|key: left|right\n"
                         "\u2022 Notes: x|`y`|z")
        self.assertEqual(rendered.count("<code>"), 3)
        self.assertNotIn("---", rendered)

    def test_even_backslashes_leave_a_real_cell_boundary(self):
        rendered = md_to_html(
            "Key | Value\n--- | ---\n" + r"path\\| three")
        self.assertEqual(html_to_plain(rendered), "\u2022 Key: path\\\n\u2022 Value: three")

    def test_empty_duplicate_and_extra_cells_are_not_dropped(self):
        rendered = md_to_html(
            "| Name | Value | Value | |\n"
            "| --- | --- | --- | --- |\n"
            "| First | 1 | 2 | |\n"
            "| Second | 3 | 4 | 5 | extra |\n"
            "| Third | 6 |"
        )
        plain = html_to_plain(rendered)
        self.assertIn("\u2022 Value: 1\n\u2022 Value: 2\n\u2022 Column 4: ", plain)
        self.assertIn("\u2022 Column 5: extra", plain)
        self.assertIn("\u2022 Name: Third\n\u2022 Value: 6\n\u2022 Value: ", plain)
        self.assertNotIn("|", plain)

    def test_explicit_single_column_table(self):
        self.assertEqual(
            html_to_plain(md_to_html("| Name |\n| --- |\n| Alpha |\n| Beta |")),
            "\u2022 Name: Alpha\n\n\u2022 Name: Beta")

    def test_ordinary_pipe_text_is_not_a_table(self):
        cases = (
            "alpha | beta",
            "alpha | beta\nsome | prose\nlast | row",
            "alpha | beta\n-- | --\nlast | row",
            "alpha | beta\n--- | --- | ---\nlast | row",
            "alpha | beta\n--- | ---",
            "Use `left|right` in the example.",
            "a `broken | b\nnot | separator",
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertNotIn("\u2022", md_to_html(source))
                self.assertEqual(html_to_plain(md_to_html(source)), source.replace("`", "")
                                 if source.count("`") == 2 else source)

    def test_table_like_code_block_remains_code(self):
        source = "Example\n```text\nA | B\n--- | ---\nC | D\n```\nEnd"
        rendered = md_to_html(source)
        self.assertIn("<pre>A | B\n--- | ---\nC | D</pre>", rendered)
        self.assertNotIn("\u2022", rendered)

    def test_table_cells_keep_links_formatting_and_escape_html(self):
        rendered = md_to_html(
            "| Label | Details |\n| --- | --- |\n"
            '| **Doc** | [*Open*](https://example.test?a=1&b=2) <b>raw</b> |')
        self.assertIn('<a href="https://example.test?a=1&amp;b=2"><i>Open</i></a>', rendered)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", rendered)


class ChunkTests(unittest.TestCase):
    def assert_chunks(self, rendered, budget):
        chunks = html_chunks(rendered, max_length=budget)
        self.assertEqual("".join(html_to_plain(chunk) for chunk in chunks),
                         html_to_plain(rendered))
        for chunk in chunks:
            parser = _BalanceChecker()
            parser.feed(chunk)
            parser.close()
            self.assertEqual(parser.stack, [])
            self.assertLessEqual(
                len(html_to_plain(chunk).encode("utf-16-le", errors="surrogatepass")) // 2,
                budget)
        return chunks

    def test_empty_exact_boundary_and_invalid_budgets(self):
        self.assertEqual(html_chunks("abc", 3), ["abc"])
        self.assertEqual(html_chunks("<b>abc</b>", 3), ["<b>abc</b>"])
        for budget in (0, -1, True, 2.5, "5", None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                html_chunks("abc", budget)

    def test_default_limit_and_long_paragraph(self):
        source = "Synthetic paragraph " * 600
        chunks = html_chunks(md_to_html(source))
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(map(html_to_plain, chunks)), source)
        self.assertTrue(all(len(html_to_plain(chunk)) <= 4000 for chunk in chunks))

    def test_long_nested_bold_code_pre_and_link(self):
        bodies = (
            "<b><i>" + "abcdef " * 40 + "</i></b>",
            "<code>" + "x_y &lt; z &amp; w " * 40 + "</code>",
            "<pre>" + "line one\n  line two\n" * 40 + "</pre>",
            '<a href="https://example.test?q=1&amp;b=2"><b>'
            + "Link label " * 40 + "</b></a>",
            "<u><s>" + "underlined and struck " * 40 + "</s></u>",
        )
        for rendered in bodies:
            with self.subTest(rendered=rendered[:40]):
                chunks = self.assert_chunks(rendered, 31)
                self.assertGreater(len(chunks), 1)
                if rendered.startswith("<a"):
                    for chunk in chunks:
                        parser = _BalanceChecker()
                        parser.feed(chunk)
                        self.assertTrue(all(
                            url == "https://example.test?q=1&b=2" for url in parser.links))

    def test_entity_spelling_is_not_split_or_counted_as_visible_text(self):
        self.assertEqual(
            html_chunks("<b>A&amp;&lt;&gt;&quot;Z</b>", 2),
            ["<b>A&amp;</b>", "<b>&lt;&gt;</b>", "<b>&quot;Z</b>"])
        self.assert_chunks("<i>&#128640;&#x1F680; &amp;amp;</i>", 3)

    def test_astral_characters_and_surrogate_pairs_stay_together(self):
        chunks = self.assert_chunks("<b>a\U0001f680b\U0001f680c</b>", 3)
        self.assertEqual(list(map(html_to_plain, chunks)), ["a\U0001f680", "b\U0001f680", "c"])
        chunks = self.assert_chunks("<b>a\ud83d\ude80b</b>", 2)
        self.assertEqual(list(map(html_to_plain, chunks)), ["a", "\ud83d\ude80", "b"])
        with self.assertRaises(ValueError):
            html_chunks("\U0001f680", 1)
        with self.assertRaises(ValueError):
            html_chunks("\ud83d", 10)

    def test_whitespace_and_blank_lines_survive_boundaries(self):
        self.assert_chunks(md_to_html("  one\n\n\n two \t three\n\n"), 4)

    def test_tables_can_cross_small_chunk_boundaries_without_losing_cells(self):
        table = "| Label | Count |\n| --- | --- |\n"
        table += "\n".join("| Item " + str(i) + " | " + str(i * 3) + " |"
                           for i in range(20))
        rendered = md_to_html(table)
        chunks = self.assert_chunks(rendered, 37)
        self.assertGreater(len(chunks), 1)
        plain = "".join(map(html_to_plain, chunks))
        self.assertIn("Item 19", plain)
        self.assertIn("Count: 57", plain)
        self.assertNotIn("|", plain)

    def test_large_href_is_markup_not_visible_budget(self):
        rendered = md_to_html("[Label](https://example.test/?q=" + "x" * 5000 + ")")
        chunks = self.assert_chunks(rendered, 3)
        self.assertEqual(list(map(html_to_plain, chunks)), ["Lab", "el"])

    def test_invalid_html_fails_explicitly(self):
        for rendered in (
            "<b>unclosed", "<b><i>crossed</b></i>", "<div>unsupported</div>",
            "<b style='x'>attributes</b>", "<br/>", "<!-- comment -->",
            '<a href="javascript:x">unsafe</a>', "&unknown;", "<!DOCTYPE html>",
            "a & b", "<incomplete", "&#0;", "&#xD800;", "&#x110000;",
        ):
            with self.subTest(rendered=rendered), self.assertRaises(ValueError):
                html_chunks(rendered)

    def test_plain_fallback_decodes_once_without_stripping_escaped_user_html(self):
        self.assertEqual(
            html_to_plain('<b>&lt;tag&gt;</b> &amp;lt; '
                          '<a href="https://example.test/?a=1&amp;b=2">link</a>'),
            "<tag> &lt; link")

    def test_synthetic_mixed_content_across_many_boundaries(self):
        rendered = md_to_html(
            "# Synthetic report\n"
            "**alpha *beta* ~~gamma~~** & <raw>\n"
            "[Docs](https://example.test?x=1&y=2) `a|b`\n"
            "| Key | Value |\n| --- | --- |\n| One | \U0001f680 |\n"
            "| Two | **end** |\n\n"
            "Example\n```text\n<&>\n  x_y\n```\n"
        )
        for budget in range(2, 50):
            with self.subTest(budget=budget):
                self.assert_chunks(rendered, budget)

    def test_deterministic_synthetic_punctuation_fuzz(self):
        random_source = random.Random(271)
        fragments = (
            "a", " ", "\n", "**", "*", "~~", "`", "``", "[", "]",
            "(https://example.test?a=1&b=2)", "|", "\\|", "<raw>",
            "---", "# ", "\\", "\U0001f680",
        )
        for case in range(200):
            source = "".join(random_source.choice(fragments)
                             for _ in range(random_source.randrange(1, 75)))
            with self.subTest(case=case):
                self.assert_chunks(md_to_html(source), random_source.randrange(2, 35))


if __name__ == "__main__":
    unittest.main()
