from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendDocument, SendMessage

from app.storage import MANIFEST_MARKER, StorageError, TelegramSnapshotBackend


class FakeBot:
    def __init__(self):
        self.next_message_id = 1
        self.messages = {}
        self.files = {}
        self.pinned_message_id = None
        self.operations = []
        self.upload_attempts = 0
        self.edit_attempts = 0
        self.upload_floods = 0
        self.edit_floods = 0

    async def get_chat(self, channel_id):
        pinned = self.messages.get(self.pinned_message_id)
        return SimpleNamespace(pinned_message=pinned)

    async def send_document(self, chat_id, document, **kwargs):
        self.upload_attempts += 1
        if self.upload_floods:
            self.upload_floods -= 1
            raise TelegramRetryAfter(
                SendDocument(chat_id=chat_id, document=document),
                "flood control",
                retry_after=34,
            )
        message_id = self.next_message_id
        self.next_message_id += 1
        file_id = f"file-{message_id}"
        self.files[file_id] = document.data
        message = SimpleNamespace(
            message_id=message_id,
            document=SimpleNamespace(file_id=file_id),
            text=None,
        )
        self.messages[message_id] = message
        self.operations.append(("upload", message_id))
        return message

    async def send_message(self, chat_id, text, **kwargs):
        message_id = self.next_message_id
        self.next_message_id += 1
        message = SimpleNamespace(message_id=message_id, document=None, text=text)
        self.messages[message_id] = message
        self.operations.append(("manifest", message_id))
        return message

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned_message_id = message_id
        self.operations.append(("pin", message_id))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edit_attempts += 1
        if self.edit_floods:
            self.edit_floods -= 1
            raise TelegramRetryAfter(
                SendMessage(chat_id=chat_id, text=text),
                "flood control",
                retry_after=34,
            )
        self.messages[message_id].text = text
        self.operations.append(("edit", message_id))

    async def delete_message(self, chat_id, message_id):
        self.messages.pop(message_id, None)
        self.operations.append(("delete", message_id))

    async def download(self, file_id, destination):
        destination.write(self.files[file_id])
        destination.seek(0)


async def test_manifest_commit_and_restart_restore():
    bot = FakeBot()
    backend = TelegramSnapshotBackend(bot, -1001, "catalog")
    payload = {"kind": "catalog", "revision": 0, "categories": {}}
    await backend.commit(payload, 0)

    assert bot.operations[:3] == [("upload", 1), ("manifest", 2), ("pin", 2)]
    assert bot.messages[2].text.startswith(MANIFEST_MARKER)

    restarted = TelegramSnapshotBackend(bot, -1001, "catalog")
    assert await restarted.load() == payload


async def test_previous_snapshot_is_used_when_current_is_corrupt():
    bot = FakeBot()
    backend = TelegramSnapshotBackend(bot, -1001, "catalog")
    old = {"kind": "catalog", "revision": 0, "value": "safe"}
    current = {"kind": "catalog", "revision": 1, "value": "new"}
    await backend.commit(old, 0)
    await backend.commit(current, 1)

    bot.files[backend.manifest.current.file_id] = b"corrupt"
    restarted = TelegramSnapshotBackend(bot, -1001, "catalog")
    restored = await restarted.load()

    assert restored == old
    assert restarted.manifest.current.revision == 0
    assert restarted.manifest.previous is None


async def test_snapshot_upload_and_manifest_commit_honor_flood_waits(monkeypatch):
    bot = FakeBot()
    bot.upload_floods = 1
    backend = TelegramSnapshotBackend(bot, -1001, "catalog")
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.storage.asyncio.sleep", no_sleep)
    await backend.commit({"kind": "catalog", "revision": 0}, 0)

    bot.edit_floods = 1
    await backend.commit({"kind": "catalog", "revision": 1}, 1)

    assert bot.upload_attempts == 3
    assert bot.edit_attempts == 2
    assert delays == [34.1, 34.1]
    assert backend.manifest.current.revision == 1


async def test_wrong_pinned_message_is_rejected():
    bot = FakeBot()
    bot.messages[1] = SimpleNamespace(message_id=1, text="human pin", document=None)
    bot.pinned_message_id = 1
    backend = TelegramSnapshotBackend(bot, -1001, "catalog")
    with pytest.raises(StorageError, match="not a valid database manifest"):
        await backend.load()
