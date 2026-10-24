"""Проверки безопасного объединения Codex hooks с профилем пользователя."""

from __future__ import annotations

import json
from pathlib import Path

from ai_engineering_memory import cli, hook_install


def test_install_merges_hooks_and_backs_up_existing_config(tmp_path):
    """Сохраняет настройки пользователя и заменяет только наши старые команды."""
    config = tmp_path / "hooks.json"
    original = {
        "custom_setting": {"enabled": True},
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo user-hook"},
                        {
                            "type": "command",
                            "command": "C:\\old\\python.exe -m ai_engineering_memory.codex_hooks UserPromptSubmit",
                        },
                        {
                            "type": "command",
                            "command": "D:\\old\\python.exe -m ai_engineering_memory.codex_hooks UserPromptSubmit",
                        },
                    ]
                }
            ],
        },
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = config.read_bytes()
    executable = str(tmp_path / "python env" / "python.exe")

    result = hook_install.install_hooks(config, executable=executable)

    assert result["changed"] is True
    assert result["backup"] is not None
    assert Path(result["backup"]).read_bytes() == original_bytes
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["custom_setting"] == {"enabled": True}
    prompt_commands = [
        handler["command"]
        for definition in saved["hooks"]["UserPromptSubmit"]
        for handler in definition.get("hooks", [])
        if handler.get("type") == "command"
    ]
    assert prompt_commands.count("echo user-hook") == 1
    installed_prompt = [
        command
        for command in prompt_commands
        if "-m ai_engineering_memory.codex_hooks UserPromptSubmit" in command
    ]
    assert len(installed_prompt) == 1
    assert executable in installed_prompt[0]
    assert len(saved["hooks"]["Stop"]) == 1


def test_install_is_idempotent_and_does_not_rewrite_current_config(tmp_path):
    """Повторный запуск не меняет файл и не создаёт лишнюю резервную копию."""
    config = tmp_path / "hooks.json"
    first = hook_install.install_hooks(config)
    contents = config.read_bytes()

    second = hook_install.install_hooks(config)

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["backup"] is None
    assert config.read_bytes() == contents


def test_install_repairs_existing_memory_hook_timeout_and_status(tmp_path):
    """Повторная установка обновляет таймауты только обработчиков AEM."""
    config = tmp_path / "hooks.json"
    executable = str(tmp_path / "python.exe")
    document = {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_install._command_for_event(
                                event, executable
                            ),
                        }
                    ]
                }
            ]
            for event in hook_install.HOOK_EVENTS
        }
    }
    document["hooks"]["UserPromptSubmit"].append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "echo preserve user hook",
                }
            ]
        }
    )
    config.write_text(json.dumps(document), encoding="utf-8")

    result = hook_install.install_hooks(config, executable=executable)

    assert result["changed"] is True
    saved = json.loads(config.read_text(encoding="utf-8"))
    for event in hook_install.HOOK_EVENTS:
        handler = saved["hooks"][event][0]["hooks"][0]
        assert handler["timeout"] == 5
        assert handler["statusMessage"] == hook_install.HOOK_STATUS_MESSAGES[event]
    assert saved["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"] == (
        "echo preserve user hook"
    )


def test_install_dry_run_does_not_create_or_change_files(tmp_path):
    """Предварительный просмотр не создаёт профиль и не трогает конфигурацию."""
    config = tmp_path / "missing" / "hooks.json"

    result = hook_install.install_hooks(config, dry_run=True)

    assert result["changed"] is True
    assert result["changes"] == {
        "UserPromptSubmit": ["registration_added"],
        "Stop": ["registration_added"],
    }
    assert result["dry_run"] is True
    assert not config.parent.exists()


def test_dry_run_reports_field_names_without_config_values(tmp_path):
    """Dry-run объясняет обновление, не раскрывая значения hooks.json."""
    config = tmp_path / "hooks.json"
    executable = str(tmp_path / "python.exe")
    original = {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_install._command_for_event(
                                event, executable
                            ),
                            "timeout": hook_install.HOOK_TIMEOUT_SECONDS,
                            "statusMessage": f"private legacy status for {event}",
                        }
                    ]
                }
            ]
            for event in hook_install.HOOK_EVENTS
        }
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    before = config.read_bytes()

    result = hook_install.install_hooks(
        config,
        executable=executable,
        dry_run=True,
    )

    assert result["changed"] is True
    assert result["changes"] == {
        "UserPromptSubmit": ["statusMessage"],
        "Stop": ["statusMessage"],
    }
    assert "private legacy status" not in json.dumps(result)
    assert config.read_bytes() == before


def test_install_rejects_malformed_json_without_overwriting_it(tmp_path):
    """Некорректная конфигурация остаётся нетронутой."""
    config = tmp_path / "hooks.json"
    config.write_text("{broken", encoding="utf-8")
    before = config.read_bytes()

    try:
        hook_install.install_hooks(config)
    except ValueError as exc:
        assert str(exc) == "Не удалось безопасно прочитать hooks.json"
    else:
        raise AssertionError("Expected malformed JSON to be rejected")

    assert config.read_bytes() == before


def test_install_rejects_duplicate_json_keys_without_overwriting(tmp_path):
    """Повторяющиеся ключи не приводят к тихой потере настроек hooks."""
    config = tmp_path / "hooks.json"
    config.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command",'
        '"command":"echo first"}]}],"Stop":[{"hooks":[]}]}}',
        encoding="utf-8",
    )
    before = config.read_bytes()

    try:
        hook_install.install_hooks(config)
    except ValueError as exc:
        assert "безопасно прочитать" in str(exc)
    else:
        raise AssertionError("Expected duplicate JSON keys to be rejected")

    assert config.read_bytes() == before
    assert not list(tmp_path.glob("hooks.json.bak-*"))


def test_install_rejects_malformed_hook_structures_without_overwriting(tmp_path):
    """Повреждённые записи hooks отклоняются до создания backup или перезаписи."""
    malformed_hooks = [
        {"Stop": ["not-an-object"]},
        {"Stop": [{"hooks": "not-a-list"}]},
        {"Stop": [{"hooks": [None]}]},
    ]

    for index, hooks in enumerate(malformed_hooks):
        config = tmp_path / f"hooks-{index}.json"
        config.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        before = config.read_bytes()

        try:
            hook_install.install_hooks(config)
        except TypeError:
            pass
        else:
            raise AssertionError("Expected malformed hook structure to be rejected")

        assert config.read_bytes() == before
        assert not list(tmp_path.glob(f"hooks-{index}.json.bak-*"))


def test_install_rejects_directory_path_without_changing_it(tmp_path):
    """Каталог с именем hooks.json не трактуется как отсутствующая конфигурация."""
    config = tmp_path / "hooks.json"
    config.mkdir()

    try:
        hook_install.install_hooks(config)
    except ValueError as exc:
        assert "не является файлом" in str(exc)
    else:
        raise AssertionError("Expected a directory path to be rejected")

    assert config.is_dir()


def test_cli_hooks_install_dry_run_uses_codex_home(tmp_path, monkeypatch, capsys):
    """CLI передаёт CODEX_HOME установщику без создания файла при dry-run."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    assert cli.main(["hooks", "install", "--dry-run"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["path"] == str(tmp_path / "hooks.json")
    assert report["changed"] is True
    assert report["changes"] == {
        "UserPromptSubmit": ["registration_added"],
        "Stop": ["registration_added"],
    }
    assert report["dry_run"] is True
    assert not (tmp_path / "hooks.json").exists()
