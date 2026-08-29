from review_agent.profiles import load_profiles, profile_skips, select_profile


def test_profile_resolves_rules_and_selects_exact_repo(tmp_path):
    config = tmp_path / "profiles.toml"
    config.write_text(
        """[[profiles]]
name = "sample"
repo = "/workspace/sample"
rules_dirs = ["rules"]
enabled_languages = ["Go"]
skip_globs = ["vendor/**", "*.generated.go"]
""",
        encoding="utf-8",
    )
    profiles = load_profiles(config)
    selected = select_profile(profiles, "/workspace/sample")
    assert selected is not None
    assert selected.enabled_languages == ("go",)
    assert selected.rules_dirs == ((tmp_path / "rules").resolve(),)
    assert profile_skips(selected, "vendor/a.go")
    assert not profile_skips(selected, "internal/a.go")
