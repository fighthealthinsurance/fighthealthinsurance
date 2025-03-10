import json
from loguru import logger
import asyncio

from channels.generic.websocket import AsyncWebsocketConsumer

from fighthealthinsurance import common_view_logic


class StreamingAppealsBackend(AsyncWebsocketConsumer):
    """Streaming back the appeals as json :D"""

    async def connect(self):
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        aitr = common_view_logic.AppealsBackendHelper.generate_appeals(data)
        async for record in aitr:
            await self.send(record)
            await self.send("\n")
        await asyncio.sleep(1)
        await self.close()


class StreamingEntityBackend(AsyncWebsocketConsumer):
    """Streaming Entity Extraction"""

    async def connect(self):
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        aitr = common_view_logic.DenialCreatorHelper.extract_entity(data["denial_id"])

        async for record in aitr:
            logger.debug(f"Sending record {record}")
            await self.send(record)
            await self.send("\n")
        await asyncio.sleep(1)
        logger.debug(f"Sent all records")
        await self.close()
