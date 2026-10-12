"""Run locally to obtain the assistant account's session string.

Never commit or send the printed string to anyone.
"""
import asyncio
from getpass import getpass

from pyrogram import Client


async def main() -> None:
    api_id = int(input("Telegram API ID: ").strip())
    api_hash = getpass("Telegram API Hash (hidden): ").strip()
    async with Client(
        "session_generator",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as client:
        session = await client.export_session_string()
        print("\nCopy the string below to Render SESSION_STRING only.")
        print("Treat it like a password. Do not paste it in chat or GitHub.\n")
        print(session)


if __name__ == "__main__":
    asyncio.run(main())
