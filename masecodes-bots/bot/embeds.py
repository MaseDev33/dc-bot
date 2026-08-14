import discord
from typing import Optional


FOOTER = "masecodes.dev"


def base_embed(title: str, description: str = "", colour: discord.Colour = discord.Colour.blue(), *, timestamp=None) -> discord.Embed:
    e = discord.Embed(title=title, description=description, colour=colour, timestamp=timestamp)
    e.set_footer(text=FOOTER)
    return e


def success_embed(title: str, description: str = "") -> discord.Embed:
    return base_embed(title, description, colour=discord.Colour.green())


def error_embed(title: str, description: str = "") -> discord.Embed:
    return base_embed(title, description, colour=discord.Colour.red())


def info_embed(title: str, description: str = "") -> discord.Embed:
    return base_embed(title, description, colour=discord.Colour.blurple())
