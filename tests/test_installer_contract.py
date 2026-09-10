from pathlib import Path


def test_display_helper_disables_and_verifies_the_output_signal():
    script = (Path(__file__).parents[1] / "start.sh").read_text(encoding="utf-8")
    assert 'xrandr --output "$output" --off' in script
    assert "still has an active signal after the off request" in script
    assert 'actual="$(vcgencmd display_power)"' in script
    assert "Firmware display power state did not become" in script


def test_repair_reinstalls_the_app_package_despite_an_unchanged_version():
    script = (Path(__file__).parents[1] / "start.sh").read_text(encoding="utf-8")
    # Services import pi-agenda from the venv's site-packages (src layout), so a
    # plain `pip install` is a no-op when the version is unchanged and updates
    # would silently keep running old code.
    assert "--force-reinstall" in script
    assert "--no-deps" in script


def test_kiosk_hides_the_pointer_and_logs_its_launch():
    script = (Path(__file__).parents[1] / "start.sh").read_text(encoding="utf-8")
    assert "unclutter -idle 0 -root" in script
    assert "pi-agenda-kiosk: launching chromium" in script


def test_scrolling_archive_is_allowed_by_the_installed_cache_csp():
    source = (
        Path(__file__).parents[1] / "src" / "pi_agenda" / "__main__.py"
    ).read_text(encoding="utf-8")
    assert "script-src 'self'" in source
