"""Unit tests for swarm_corp core functions."""

from swarm_corp import estimate_tokens, extract_verdict, _find_truncation_point


class TestEstimateTokens:
    """Verify token estimation is conservative (rounds up)."""

    def test_short_text(self):
        """Short text should estimate 1+ tokens."""
        assert estimate_tokens("") >= 1
        assert estimate_tokens("hello") >= 1

    def test_conservatism(self):
        """Estimate should be conservative (pessimistic)."""
        # Real: "hello world" is ~2 tokens; our estimate should be >= 2
        text = "hello world"
        estimate = estimate_tokens(text)
        assert estimate >= len(text) // 4  # our formula: ceil(len/3) >= len/4


class TestExtractVerdict:
    """Verify verdict parsing handles <think> and finds last verdict."""

    def test_simple_approve(self):
        """Extract simple APPROVE."""
        text = "VERDICT: APPROVE\n\nLooks good."
        verdict, reasoning = extract_verdict(text)
        assert verdict == "APPROVE"
        assert "Looks good" in reasoning

    def test_simple_reject(self):
        """Extract simple REJECT."""
        text = "VERDICT: REJECT\n\nMissing tests."
        verdict, reasoning = extract_verdict(text)
        assert verdict == "REJECT"

    def test_strips_think_blocks(self):
        """<think> blocks are stripped before parsing."""
        text = "<think>\nVERDICT: REJECT (during thought)\n</think>\nVERDICT: APPROVE\n\nActual decision."
        verdict, reasoning = extract_verdict(text)
        assert verdict == "APPROVE"  # finds last, not first
        assert "<think>" not in reasoning

    def test_unparseable(self):
        """Missing VERDICT: line returns UNPARSEABLE."""
        text = "This is just commentary, no verdict."
        verdict, reasoning = extract_verdict(text)
        assert verdict == "UNPARSEABLE"

    def test_last_verdict_wins(self):
        """If multiple VERDICT: lines, last one is used."""
        text = "VERDICT: REJECT (initial thought)\nVERDICT: APPROVE (revised)"
        verdict, reasoning = extract_verdict(text)
        assert verdict == "APPROVE"


class TestTruncation:
    """Verify intelligent truncation finds good break points."""

    def test_no_truncation_needed(self):
        """Text shorter than max doesn't get truncated."""
        text = "short text"
        point = _find_truncation_point(text, 100)
        assert point == len(text)

    def test_paragraph_break_preferred(self):
        """Truncation prefers paragraph breaks."""
        text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"
        # Break at 90% of max (25 chars = 22.5) should find \n\n at position 15
        point = _find_truncation_point(text, 25)
        assert point > 0 and point <= 25
        # Should prefer \n\n over single \n
        assert text[:point] == "first paragraph"

    def test_line_break_fallback(self):
        """If no paragraph break, use line break."""
        text = "line 1\nline 2\nline 3"
        point = _find_truncation_point(text, 10)
        assert text[:point] == "line 1"
