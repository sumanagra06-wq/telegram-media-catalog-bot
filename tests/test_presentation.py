from app.models import UserProfile, UserStatus
from app.presentation import ActionButton, infer_button_style
from app.ui import admin_dashboard, main_dashboard, watchlist_home, watchlist_status_picker


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _by_callback(markup):
    return {button.callback_data: button for button in _buttons(markup)}


def test_semantic_style_inference_reserves_neutral_for_secondary_actions():
    assert infer_button_style("🔎 Search", "menu:search") == "primary"
    assert infer_button_style("🗂 Categories", "admin:categories") == "primary"
    assert infer_button_style("✅ Approve", "aus:42:active") == "success"
    assert infer_button_style("📤 Publish database", "admin:publish") == "success"
    assert infer_button_style("📤 Export backup", "adb:backup") == "success"
    assert infer_button_style("🗑 Permanently delete 4 files", "adrx:c_1") == "danger"
    assert infer_button_style("⛔ Ban user", "aus:42:banned") == "danger"

    assert infer_button_style("✖️ Cancel", "adrx:c_1") is None
    assert infer_button_style("◀️ Back", "adrx:c_1") is None
    assert infer_button_style("◀️ Previous", "sr:token:0") is None
    assert infer_button_style("⏸ Disable", "act:c_1") is None
    assert infer_button_style("⏸ Suspend", "aus:42:suspended") is None


def test_action_button_serializes_official_style_and_supports_neutral_override():
    button = ActionButton(text="🔎 Search", callback_data="menu:search")
    payload = button.model_dump(exclude_none=True)
    assert payload == {
        "text": "🔎 Search",
        "style": "primary",
        "callback_data": "menu:search",
    }

    neutral = ActionButton(text="Delete", callback_data="dangerous", style=None)
    assert neutral.style is None
    assert "style" not in neutral.model_dump(exclude_none=True)


def test_style_is_optional_metadata_and_does_not_change_the_action_contract():
    styled = ActionButton(
        text="🗑 Permanently delete",
        callback_data="adrx:c_example",
        style="danger",
    )
    payload = styled.model_dump(exclude_none=True)
    unstyled_payload = {key: value for key, value in payload.items() if key != "style"}

    assert unstyled_payload == {
        "text": "🗑 Permanently delete",
        "callback_data": "adrx:c_example",
    }


def test_primary_dashboards_use_semantic_colors_without_custom_emoji_dependencies():
    home_text, home_markup = main_dashboard(is_owner=True, first_name="A & B")
    home = _by_callback(home_markup)
    assert "<blockquote>" in home_text
    assert "A &amp; B" in home_text
    assert home["menu:search"].style == "primary"
    assert home["menu:browse"].style == "primary"
    assert home["menu:watchlist"].style == "primary"
    assert home["menu:help"].style is None
    assert home["admin:home"].style == "primary"

    user = UserProfile(
        telegram_user_id=42,
        first_name="Alice",
        status=UserStatus.ACTIVE,
        watchlist_public=False,
    )
    watchlist_text, watchlist_markup = watchlist_home(user)
    watchlist = _by_callback(watchlist_markup)
    assert "MY WATCHLIST" in watchlist_text
    assert watchlist["wla:start"].style == "success"
    assert watchlist["wlm:0"].style == "primary"
    assert watchlist["wlvis:1"].style == "success"
    assert watchlist["menu:home"].style is None

    user.watchlist_public = True
    public_watchlist = _by_callback(watchlist_home(user)[1])
    assert public_watchlist["wlvis:0"].style is None
    status_picker = _by_callback(watchlist_status_picker("Arrival", "wams")[1])
    assert status_picker["menu:watchlist"].style is None

    _, admin_markup = admin_dashboard()
    admin = _by_callback(admin_markup)
    assert admin["admin:categories"].style == "primary"
    assert admin["admin:users"].style == "primary"
    assert admin["menu:home"].style is None

    for markup in (home_markup, watchlist_markup, admin_markup):
        assert all(button.icon_custom_emoji_id is None for button in _buttons(markup))
        assert all(
            button.style in {None, "primary", "success", "danger"} for button in _buttons(markup)
        )


def test_callback_contracts_on_main_dashboard_are_unchanged():
    _, markup = main_dashboard(is_owner=True)
    assert set(_by_callback(markup)) == {
        "menu:search",
        "menu:browse",
        "menu:recent",
        "menu:watchlist",
        "menu:help",
        "admin:home",
    }
