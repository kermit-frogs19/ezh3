# This is a sample Python script.

# Press Shift+F10 to execute it or replace it with your code.
# Press Double Shift to search everywhere for classes, files, tool windows, actions, and settings.

import asyncio
from server.test import Server, ServerRequest


app = Server(
    enable_tls=True,
    cert_type="CUSTOM",
    custom_cert_file_loc="/etc/letsencrypt/live/vadim-seliukov-quic-server.com/fullchain.pem",
    custom_cert_key_file_loc="/etc/letsencrypt/live/vadim-seliukov-quic-server.com/privkey.pem"
)


@app.get("/")
async def home():
    return {"message": "Welcome to QUIC Server"}


@app.post("/echo")
async def echo(request: ServerRequest):
    data = request.json()
    return data


async def main1():
    await app.run(port=443, host="0.0.0.0")


if __name__ == '__main__':
    asyncio.run(main1())

# See PyCharm help at https://www.jetbrains.com/help/pycharm/
