import asyncio
import json


class Ops:
    def __init__(self, path):
        self.path = path

    async def call(self, payload):
        if not self.path:
            return {'result': 'unavailable'}
        writer = None
        try:
            async with asyncio.timeout(55 if payload['action'] == 'restart' else 8):
                reader, writer = await asyncio.open_unix_connection(self.path, limit=16384)
                writer.write(json.dumps(payload).encode()+b'\n')
                await writer.drain()
                raw = await reader.readline()
                result = json.loads(raw)
                return result if isinstance(result, dict) else {'result': 'unavailable'}
        except (OSError, ValueError, TimeoutError):
            return {'result': 'unknown' if payload['action'] == 'restart' else 'unavailable'}
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def status(self):
        return await self.call({'action': 'status'})

    async def restart(self, target, request_id):
        return await self.call({'action': 'restart', 'target': target, 'id': request_id})
