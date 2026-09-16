import unittest

from reading_exact import body_similarity, copies_are_effectively_exact, text_fingerprint, titles_match


class ReadingExactTests(unittest.TestCase):
    def test_title_matching_ignores_spacing_and_punctuation(self):
        self.assertTrue(titles_match('3000万的彩礼究竟算不算天价？', '3000万的彩礼，究竟算不算天价?'))

    def test_exact_copy_tolerates_whitespace_only(self):
        a='\n\n'.join(['第一段。', '第二段。'] * 80)
        b='\r\n'.join(['  第一段。  ', '第二段。'] * 80)
        self.assertTrue(copies_are_effectively_exact(a, b))
        self.assertEqual(text_fingerprint(a), text_fingerprint(b))

    def test_rewrite_is_not_exact(self):
        a='这是作者原文的一整段内容。' * 80
        b='这是别人重新概括后的不同内容。' * 80
        self.assertLess(body_similarity(a, b), 0.995)
        self.assertFalse(copies_are_effectively_exact(a, b))


if __name__ == '__main__':
    unittest.main()
