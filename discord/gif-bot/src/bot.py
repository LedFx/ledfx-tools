from dotenv import load_dotenv
import discord
from discord import app_commands

import io
import os
import requests
import paramiko


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SFTP_HOST = os.getenv("SFTP_HOST")
SFTP_USERNAME = os.getenv("SFTP_USERNAME")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD")
SFTP_PATH = os.getenv("SFTP_PATH")
GIFS_FOLDER_URL = os.getenv("GIFS_FOLDER_URL")
LEDFX_SERVER_GUILD = os.getenv("LEDFX_SERVER_GUILD")


intents = discord.Intents.default()
intents.messages = True
intents.reactions = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def on_ready():
    print(f"{client.user} has connected to Discord!")


@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=LEDFX_SERVER_GUILD))


@tree.command(
    name="addgif",
    description="Add a gif to LedFx assets",
    guild=discord.Object(id=LEDFX_SERVER_GUILD),
)
async def add_gif(interaction, name: str, url: str):
    channel = interaction.channel
    user_id = interaction.user.id
    RAW_GIF_NAME = name.strip().lower().replace(" ", "_")
    if not RAW_GIF_NAME.endswith(".gif"):
        GIF_NAME = RAW_GIF_NAME + ".gif"
    else:
        GIF_NAME = RAW_GIF_NAME

    GIF_URL = url

    await check_existing_file(channel, user_id, GIF_NAME)
    await check_gif_details(interaction, GIF_URL, GIF_NAME)
    await download_gif(channel, user_id, GIF_URL, GIF_NAME)
    await upload_gif(channel, user_id, GIF_NAME)


async def check_existing_file(channel, user_id, GIF_NAME):
    existing_file_url = f"https://assets.ledfx.app/gifs/{GIF_NAME}"
    response = requests.head(existing_file_url)
    if response.status_code == 200:
        await channel.send(
            f"A file with the same name already exists: {existing_file_url}"
        )
        return


async def check_gif_details(interaction, GIF_URL, GIF_NAME):
    try:
        quick_gif_check = requests.head(GIF_URL)
        quick_content_type = quick_gif_check.headers.get("content-type")
        if quick_gif_check and quick_content_type == "image/gif":
            await interaction.response.send_message(
                f"Attempting to upload {GIF_NAME} to assets..."
            )
        else:
            await interaction.response.send_message(
                f"Is this accessible/a GIF? Content Type: {quick_content_type}, Server Response: {quick_content_type.status_code}"
            )
            return
    except Exception as e:
        await interaction.response.send_message(
            f"Failed to get details of the file from {GIF_URL}: {e}"
        )
        return


async def download_gif(channel, user_id, GIF_URL, GIF_NAME):
    try:
        response = requests.get(GIF_URL)
    except Exception as e:
        await channel.send(
            f"<@{user_id}>: Failed to download the file from {GIF_URL}: {e}"
        )
        return

    if response.status_code != 200:
        await channel.send(
            f"<@{user_id}>: Failed to download the file from {GIF_URL}: {response.status_code}"
        )
        return

    try:
        file_obj = io.BytesIO()
        file_obj.write(response.content)
        file_obj.seek(0)

        transport = paramiko.Transport((SFTP_HOST, 22))
        transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.putfo(fl=file_obj, remotepath=f"{SFTP_PATH}/{GIF_NAME}")
        sftp.close()
        transport.close()
        await channel.send(
            f"<@{user_id}>: New GIF added: https://assets.ledfx.app/gifs/{GIF_NAME}"
        )
    except Exception as e:
        await channel.send(f"<@{user_id}>: Failed to upload the last GIF: {str(e)}")


client.run(DISCORD_TOKEN)
