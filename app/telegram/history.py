import secrets

from redis.asyncio import Redis

# Only the owner may release a lock; an expired request cannot unlock a new one.
RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class HistoryStore:
    def __init__(self, redis: Redis, history_ttl: int, lock_ttl: int):
        self.redis = redis
        self.history_ttl = history_ttl
        self.lock_ttl = lock_ttl

    async def acquire(self, session: str) -> str | None:
        token = secrets.token_hex(16)
        acquired = await self.redis.set(f"lock:{session}", token, ex=self.lock_ttl, nx=True)
        return token if acquired else None

    async def release(self, session: str, token: str) -> None:
        await self.redis.eval(RELEASE_LOCK, 1, f"lock:{session}", token)

    async def read(self, session: str) -> list[str]:
        return await self.redis.lrange(f"history:{session}", 0, -1)

    async def append(self, session: str, question: str, answer: str) -> None:
        key = f"history:{session}"
        async with self.redis.pipeline(transaction=True) as transaction:
            transaction.rpush(
                key, f"Пользователь: {question}"[:4000], f"Ассистент: {answer}"[:4000]
            )
            transaction.ltrim(key, -10, -1)
            transaction.expire(key, self.history_ttl)
            await transaction.execute()

    async def clear(self, session: str) -> None:
        await self.redis.delete(f"history:{session}")
