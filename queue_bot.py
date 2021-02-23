import re
import discord

import database as db


async def create_temp_channel(guild: discord.Guild, name: str, channel_type, category=None, position: int = None,
                              overwrites=None, parent_id=None):
    if channel_type == "voice":
        channel = await guild.create_voice_channel(name, category=category, position=position, overwrites=overwrites)
    else:
        channel = await guild.create_text_channel(name, category=category, position=position, overwrites=overwrites)

    if parent_id is None:
        db.update(db.temp_channel_ref(guild.id, channel.id), db.Key.related, [])
    else:
        db.append_array(db.temp_channel_ref(guild.id, parent_id), db.Key.related, channel.id)

    return channel


async def delete_temp_channel(guild: discord.Guild, channel_id: int):
    channel = guild.get_channel(channel_id)
    if channel is not None:
        await channel.delete()
    temp_ref = db.temp_channel_ref(guild.id, channel_id)
    if (temp := temp_ref.get()) is None:
        return
    try:
        related_channel_ids = temp.to_dict()[db.Key.related.name]
        for related_channel_id in related_channel_ids:
            related_channel = guild.get_channel(related_channel_id)
            if related_channel is not None:
                await related_channel.delete()
    except KeyError:
        pass
    temp_ref.delete()


def room_name(username: str):
    if username[-1] == "s":
        return f"{username}' Room"
    else:
        return f"{username}'s Room"


def queue_role_name(name: str):
    return f"{name.title()} Queue"


async def create_queue_role(guild: discord.Guild, queue_id: int):
    queue_name = db.get(db.queue_ref(guild.id, queue_id), db.Key.name)
    role = await guild.create_role(name=queue_role_name(queue_name), hoist=True)

    for queue_channel in db.queues_ref(guild.id).stream():
        channel = guild.get_channel(int(queue_channel.id))
        await channel.set_permissions(role, connect=False)

    return role


async def new_queue(ctx, name: str):
    channel = await ctx.guild.create_voice_channel(f"➕ Join {name} queue...")
    db.update(db.queue_ref(ctx.guild.id, channel.id), db.Key.name, name)
    await create_queue_role(ctx.guild, channel.id)


async def open_queue(ctx):
    for queue_channel in db.queues_ref(ctx.guild.id).stream():
        channel = ctx.guild.get_channel(int(queue_channel.id))
        await channel.set_permissions(ctx.guild.default_role, overwrite=None)
        await queue_update(ctx.guild, int(queue_channel.id))

    await delete_queue_status_message(ctx.guild)
    db.update(db.guild_ref(ctx.guild.id), db.Key.queue_status, True)

    embed = discord.Embed(title=u"Join the voice channel for the queue you want", colour=discord.Colour.blue(),
                          description="Once in your waiting room feel free to join someone else's while you wait.")
    embed.set_author(name="Queues are Open!", icon_url="https://cdn.discordapp.com/icons/812343984294068244/69241d42f3661678d61b3af3cfb04f45.png")
    embed.set_footer(text="If you leave a voice channel in this server you will be removed from the queue!")

    message = await ctx.send(embed=embed)
    await message.pin()
    db.update(db.guild_ref(ctx.guild.id), db.Key.queue_status_message, [ctx.channel.id, message.id])


async def close_queue(ctx):
    for queue_channel in db.queues_ref(ctx.guild.id).stream():
        channel = ctx.guild.get_channel(int(queue_channel.id))
        await channel.set_permissions(ctx.guild.default_role, connect=False)

    await delete_queue_status_message(ctx.guild)
    db.update(db.guild_ref(ctx.guild.id), db.Key.queue_status, False)

    embed = discord.Embed(title=u"Come back next time", colour=discord.Colour.blue(),
                          description="Turn on notifications for this channel to be notified when the queues open again!")
    embed.set_author(name="Queues are Closed", icon_url="https://cdn.discordapp.com/icons/812343984294068244/69241d42f3661678d61b3af3cfb04f45.png")

    message = await ctx.send(embed=embed)
    await message.pin()
    db.update(db.guild_ref(ctx.guild.id), db.Key.queue_status_message, [ctx.channel.id, message.id])


async def queue_update(guild: discord.Guild, queue_id: int):
    queue = db.get(db.queue_ref(guild.id, queue_id), db.Key.queue, default=[])
    queue_name = db.get(db.queue_ref(guild.id, queue_id), db.Key.name, default="Lab")
    queue_update_channel = guild.get_channel(db.get(db.guild_ref(guild.id), db.Key.queue_updates_channel))

    await delete_queue_update_message(guild, queue_id)

    embed = discord.Embed(title=u"Next in queue:᲼᲼᲼᲼᲼᲼᲼᲼᲼᲼᲼᲼", colour=discord.Colour.blue())
    embed.set_author(name=f"{queue_name.title()} Queue", icon_url="https://cdn.discordapp.com/icons/812343984294068244/69241d42f3661678d61b3af3cfb04f45.png")

    if len(queue) > 0:
        regex = re.compile(r" \(\d+\)")
        user = await guild.fetch_member(queue[0])
        embed.description = user.display_name
        embed.set_thumbnail(url=user.avatar_url)
        embed.set_footer(text="To move them to your room click ✅")

        for i in range(len(queue)):
            user = await guild.fetch_member(queue[i])
            await update_queue_position(user, i + 1, regex=regex)
    else:
        embed.description = "The queue is empty."

    message = await queue_update_channel.send(embed=embed)
    if len(queue) > 0:
        await message.add_reaction("✅")

    db.update(db.queue_ref(guild.id, queue_id), db.Key.queue_update_message, [queue_update_channel.id, message.id])


async def update_queue_position(user: discord.Member, position: int, regex=re.compile(r" \(\d+\)")):
    try:
        if position == 0:
            await user.edit(nick=f"{regex.sub('', user.display_name)}")
        else:
            await user.edit(nick=f"{regex.sub('', user.display_name)} ({position})")
    except discord.errors.Forbidden:
        pass


async def on_queue_message_react(reaction: discord.Reaction, user: discord.Member):
    queues = db.queues_ref(user.guild.id).where(db.Key.queue_update_message.name, "==",
                                                [reaction.message.channel.id, reaction.message.id]).stream()
    queue_id = None
    for queue in queues:
        queue_id = int(queue.id)
        break

    if queue_id is None:
        return

    if user.voice is None:
        await reaction.message.channel.send("❌ You must be in a voice channel!", delete_after=5)
        await reaction.remove(user)
    else:
        await reaction.message.delete()
        queue_ref = db.queue_ref(user.guild.id, queue_id)
        queue = db.get(queue_ref, db.Key.queue, default=[])
        db.remove_array(queue_ref, db.Key.queue, queue[0])

        user_waiting = await user.guild.fetch_member(queue[0])
        await user_waiting.edit(voice_channel=user.voice.channel)
        await update_queue_position(user_waiting, 0)
        await queue_update(user.guild, queue_id)

        related_channel_ids = db.get(db.temp_channel_ref(user.guild.id, user.voice.channel.id), db.Key.related, [])

        for related_channel_id in related_channel_ids:
            related_channel = user.guild.get_channel(related_channel_id)
            await related_channel.set_permissions(user_waiting, view_channel=True, send_messages=True)

        role_name = queue_role_name(db.get(db.queue_ref(user.guild.id, queue_id), db.Key.name, ""))
        role = discord.utils.get(user.guild.roles, name=role_name)
        if role is not None:
            await user_waiting.remove_roles(role)


async def delete_queue_status_message(guild: discord.Guild):
    old_message = db.get(db.guild_ref(guild.id), db.Key.queue_status_message, [0, 0])
    channel = guild.get_channel(old_message[0])
    if channel is not None:
        try:
            await delete_message(await channel.fetch_message(old_message[1]))
        except discord.errors.NotFound:
            pass


async def delete_queue_update_message(guild: discord.Guild, queue_id: int):
    old_message = db.get(db.queue_ref(guild.id, queue_id), db.Key.queue_update_message, [0, 0])
    channel = guild.get_channel(old_message[0])
    if channel is not None:
        try:
            await delete_message(await channel.fetch_message(old_message[1]))
        except discord.errors.NotFound:
            pass


async def delete_message(message: discord.Message):
    if message is not None:
        await message.delete()
