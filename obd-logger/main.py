import asyncio
import uvicorn
from can_poller import start_poller
from server import app


async def main():
    asyncio.create_task(start_poller())
    config = uvicorn.Config(app, host='0.0.0.0', port=8000, log_level='info')
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == '__main__':
    asyncio.run(main())
