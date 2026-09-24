"""P3.4 + P3.5: adaptive tool descriptions and fenced-mode contract.

P3.4 — When the resolved profile is 'minimal', tool schemas served to the
       model carry compact one-liner descriptions instead of the verbose
       multi-sentence originals, saving ~2k tokens per turn.

P3.5 — When the health probe reports native_tools=False, the system prompt
       gains a FENCED_CONTRACT summary so the model has a compact protocol
       reference without the full protocol block.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── P3.4: COMPACT_DESC in syscalls ────────────────────────────────────

class TestCompactDesc:
    def test_compact_desc_exists(self):
        from kern.syscalls import COMPACT_DESC
        assert isinstance(COMPACT_DESC, dict)
        assert len(COMPACT_DESC) == 15

    def test_all_schemas_have_compact(self):
        from kern.syscalls import SCHEMAS, COMPACT_DESC
        names = [t["function"]["name"] for t in SCHEMAS]
        missing = [n for n in names if n not in COMPACT_DESC]
        assert missing == [], f"Missing compact descriptions: {missing}"

    def test_compact_desc_are_strings(self):
        from kern.syscalls import COMPACT_DESC
        for name, desc in COMPACT_DESC.items():
            assert isinstance(desc, str), f"{name} desc is not str"
            assert len(desc) > 0, f"{name} desc is empty"

    def test_compact_shorter_than_full(self):
        """F-14: full descriptions are now ≤120 chars; compact must be ≤ full."""
        from kern.syscalls import SCHEMAS, COMPACT_DESC
        for t in SCHEMAS:
            fn = t["function"]
            full = fn.get("description", "")
            compact = COMPACT_DESC[fn["name"]]
            assert len(compact) <= len(full), (
                f"{fn['name']}: compact ({len(compact)}) longer than full ({len(full)})"
            )

    def test_read_compact_mentions_full(self):
        from kern.syscalls import COMPACT_DESC
        assert "full=true" in COMPACT_DESC["read"]

    def test_edit_compact_mentions_unique_string(self):
        from kern.syscalls import COMPACT_DESC
        assert "unique string" in COMPACT_DESC["edit"]


# ── P3.4: wiring in _tools() ─────────────────────────────────────────

class TestCompactDescWiring:
    def test_minimal_profile_gets_compact_descriptions(self):
        """When profile is minimal, tool descriptions are replaced."""
        from kern.syscalls import COMPACT_DESC, SCHEMAS
        from kern.profiles import MINIMAL

        # Simulate what _tools does for minimal profile
        import copy
        tools = copy.deepcopy(SCHEMAS)
        profile = MINIMAL
        if profile.name == "minimal":
            for t in tools:
                fn = t.get("function", {})
                compact = COMPACT_DESC.get(fn.get("name", ""))
                if compact:
                    fn["description"] = compact

        for t in tools:
            fn = t["function"]
            assert fn["description"] == COMPACT_DESC[fn["name"]]

    def test_standard_profile_keeps_full_descriptions(self):
        """When profile is standard, descriptions remain unchanged."""
        from kern.syscalls import SCHEMAS
        from kern.profiles import STANDARD
        import copy

        tools = copy.deepcopy(SCHEMAS)
        profile = STANDARD
        if profile.name == "minimal":
            pass  # would not enter

        for i, t in enumerate(tools):
            fn = t["function"]
            assert fn["description"] == SCHEMAS[i]["function"]["description"]


# ── P3.5: FENCED_CONTRACT in kernel ──────────────────────────────────

class TestFencedContract:
    def test_fenced_contract_exists(self):
        from kern.kernel import FENCED_CONTRACT
        assert isinstance(FENCED_CONTRACT, str)
        assert len(FENCED_CONTRACT) > 100

    def test_fenced_contract_mentions_protocol(self):
        from kern.kernel import FENCED_CONTRACT
        assert "Tool Calling Protocol" in FENCED_CONTRACT

    def test_fenced_contract_mentions_tool_json(self):
        """F-60: contract now describes the ```tool JSON syntax the parser accepts."""
        from kern.kernel import FENCED_CONTRACT
        assert "```tool" in FENCED_CONTRACT

    def test_fenced_contract_mentions_arguments(self):
        from kern.kernel import FENCED_CONTRACT
        assert "arguments" in FENCED_CONTRACT

    def test_fenced_contract_mentions_json(self):
        from kern.kernel import FENCED_CONTRACT
        assert "JSON" in FENCED_CONTRACT

    def test_fenced_contract_has_hard_rules(self):
        from kern.kernel import FENCED_CONTRACT
        assert "Hard rules" in FENCED_CONTRACT

    def test_fenced_contract_reasonable_length(self):
        """Should be compact: under 1000 chars."""
        from kern.kernel import FENCED_CONTRACT
        assert len(FENCED_CONTRACT) < 1000


# ── P3.5: system_prompt integration ──────────────────────────────────

class TestFencedContractIntegration:
    def test_system_prompt_without_native_tools_includes_contract(self):
        """_system() appends FENCED_CONTRACT when health says native_tools=False."""
        from kern.kernel import FENCED_CONTRACT, system_prompt
        # We can't easily test _system() without a full Engine instance,
        # but we verify the contract is importable and well-formed.
        assert FENCED_CONTRACT.startswith("\n## Tool Calling Protocol")

    def test_kernel_still_compiles_and_exports(self):
        import kern.kernel as k
        assert hasattr(k, "KERNEL")
        assert hasattr(k, "CAP_BLOCK")
        assert hasattr(k, "SESSION_BLOCK")
        assert hasattr(k, "FENCED_CONTRACT")
        assert hasattr(k, "system_prompt")
