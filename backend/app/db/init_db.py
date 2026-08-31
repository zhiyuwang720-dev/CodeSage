"""
数据库初始化模块（Phase 1 L4 精简版）
在应用启动时创建默认演示账户。
legacy 演示数据种子（AuditTask/AuditIssue/InstantAnalysis）随 legacy 引擎一并裁剪。
"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.security import get_password_hash
from app.models.user import User

logger = logging.getLogger(__name__)

# 默认演示账户配置
DEFAULT_DEMO_EMAIL = "demo@example.com"
DEFAULT_DEMO_PASSWORD = "demo123"
DEFAULT_DEMO_NAME = "演示用户"


async def create_demo_user(db: AsyncSession) -> User | None:
    """
    创建演示用户账户
    - demo@example.com / demo123
    """
    result = await db.execute(select(User).where(User.email == DEFAULT_DEMO_EMAIL))
    demo_user = result.scalars().first()

    if not demo_user:
        demo_user = User(
            email=DEFAULT_DEMO_EMAIL,
            hashed_password=get_password_hash(DEFAULT_DEMO_PASSWORD),
            full_name=DEFAULT_DEMO_NAME,
            is_active=True,
            is_superuser=True,  # 演示用户拥有管理员权限以便体验所有功能
            role="admin",
        )
        db.add(demo_user)
        await db.flush()
        logger.info(f"✓ 创建演示账户: {DEFAULT_DEMO_EMAIL}")
        return demo_user
    else:
        logger.info(f"演示账户已存在: {DEFAULT_DEMO_EMAIL}")
        return demo_user


async def init_db(db: AsyncSession) -> None:
    """初始化数据库：创建演示账户（表结构由 alembic 迁移或 create_all 负责）"""
    await create_demo_user(db)
    await db.commit()
