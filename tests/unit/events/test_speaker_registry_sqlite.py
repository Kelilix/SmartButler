"""SQLiteSpeakerRegistry 单元测试。"""

from __future__ import annotations

import tempfile
from datetime import datetime, UTC
from pathlib import Path

import pytest

from smartbutler.events.routing.speaker_registry import SpeakerProfile
from smartbutler.events.routing.speaker_registry_sqlite import SQLiteSpeakerRegistry


class DummyEmbedder:
    """假声纹 embedder，单测用。"""

    NATIVE_DIM = 512

    async def embed(self, audio, sample_rate: int = 16000):
        # 返回固定 embedding
        return [0.1] * 512

    async def close(self):
        pass


@pytest.fixture
def db_path():
    """临时 SQLite 数据库路径。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)
    # cleanup
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
async def registry(db_path):
    """带清理的 registry 实例。"""
    embedder = DummyEmbedder()
    reg = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg.start()
    yield reg
    await reg.stop()


@pytest.mark.asyncio
async def test_register_and_identify(db_path):
    """测试注册后能正确识别。"""
    embedder = DummyEmbedder()
    reg = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg.start()

    # 注册一个 profile
    profile = SpeakerProfile(
        user_id="test_user",
        display_name="测试用户",
        embedding=[0.1] * 512,
        enrolled_at=datetime.now(UTC),
        sample_count=3,
    )
    await reg.register(profile)

    # 识别（相同 embedding → 高相似度）
    match = await reg.identify([0.1] * 512)
    assert match.is_known is True
    assert match.matched_user_id == "test_user"
    assert match.confidence == pytest.approx(1.0)

    await reg.stop()


@pytest.mark.asyncio
async def test_persistence_after_restart(db_path):
    """测试重启后数据从 SQLite 恢复。"""
    embedder = DummyEmbedder()

    # 第一次：注册
    reg1 = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg1.start()
    profile = SpeakerProfile(
        user_id="persistent_user",
        display_name="持久化用户",
        embedding=[0.2] * 512,
        enrolled_at=datetime.now(UTC),
        sample_count=5,
    )
    await reg1.register(profile)
    await reg1.stop()

    # 第二次：从 SQLite 加载（不重新注册）
    reg2 = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg2.start()

    loaded = await reg2.get("persistent_user")
    assert loaded is not None
    assert loaded.user_id == "persistent_user"
    assert loaded.display_name == "持久化用户"
    assert loaded.sample_count == 5
    # embedding 应该相同
    assert loaded.embedding == [0.2] * 512

    await reg2.stop()


@pytest.mark.asyncio
async def test_unregister(db_path):
    """测试注销。"""
    embedder = DummyEmbedder()
    reg = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg.start()

    profile = SpeakerProfile(
        user_id="to_remove",
        display_name="待删除",
        embedding=[0.3] * 512,
        enrolled_at=datetime.now(UTC),
    )
    await reg.register(profile)

    # 确认存在
    assert await reg.get("to_remove") is not None

    # 注销
    deleted = await reg.unregister("to_remove")
    assert deleted is True
    assert await reg.get("to_remove") is None

    await reg.stop()


@pytest.mark.asyncio
async def test_list_members(db_path):
    """测试列出所有成员。"""
    embedder = DummyEmbedder()
    reg = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg.start()

    for i in range(3):
        profile = SpeakerProfile(
            user_id=f"user_{i}",
            display_name=f"用户{i}",
            embedding=[0.1 + i * 0.01] * 512,
            enrolled_at=datetime.now(UTC),
        )
        await reg.register(profile)

    members = await reg.list_members()
    assert len(members) == 3
    user_ids = {m.user_id for m in members}
    assert user_ids == {"user_0", "user_1", "user_2"}

    await reg.stop()


@pytest.mark.asyncio
async def test_duplicate_user_id_raises(db_path):
    """测试重复 user_id 抛 ValueError。"""
    embedder = DummyEmbedder()
    reg = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg.start()

    profile1 = SpeakerProfile(
        user_id="dup",
        display_name="第一个",
        embedding=[0.5] * 512,
        enrolled_at=datetime.now(UTC),
    )
    await reg.register(profile1)

    profile2 = SpeakerProfile(
        user_id="dup",
        display_name="第二个",
        embedding=[0.6] * 512,
        enrolled_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="已注册"):
        await reg.register(profile2)

    await reg.stop()


@pytest.mark.asyncio
async def test_identify_unknown_speaker(db_path):
    """测试陌生人识别。"""
    embedder = DummyEmbedder()
    reg = SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path)
    await reg.start()

    # 不注册任何成员，识别陌生人
    match = await reg.identify([0.9] * 512)
    assert match.is_known is False
    assert match.matched_user_id is None

    await reg.stop()


@pytest.mark.asyncio
async def test_context_manager(db_path):
    """测试 async context manager。"""
    embedder = DummyEmbedder()
    async with SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path) as reg:
        profile = SpeakerProfile(
            user_id="cm_user",
            display_name="上下文管理器用户",
            embedding=[0.7] * 512,
            enrolled_at=datetime.now(UTC),
        )
        await reg.register(profile)

    # 退出后连接已关闭，再次启动应能加载数据
    async with SQLiteSpeakerRegistry(embedder=embedder, db_path=db_path) as reg2:
        loaded = await reg2.get("cm_user")
        assert loaded is not None
        assert loaded.user_id == "cm_user"
