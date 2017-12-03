import bot
import datetime
import emoji
import asyncio
import pymysql
import re
from secret import *
bot = bot.MusicBot()


async def check_users(channel, server, date):
    server_name = str(server.name.lower())  # sql only accepts servers with lower case names
    server_name = re.escape(emoji.demojize(server_name))  # escaping dumbass emojis and punctuation
    conn = pymysql.connect(
        host='localhost',
        port=3306,
        user='root',
        passwd=mysql_password,
        db='discord_user_backup'
    )
    cur = conn.cursor()
    check_table = "SELECT count(*) FROM information_schema.TABLES WHERE table_name = '{}'".format(
        re.escape(server_name))
    cur.execute(check_table)
    result = cur.fetchone()
    if '0' in str(result):
        create_table = "CREATE TABLE `{}`(`channel_name` TEXT, `channel_type` TEXT , `date` VARCHAR(255))".format(
            server_name)
        cur.execute(create_table)
        await bot.safe_send_message(channel,
                               "{} was not found on the database but a table for it was successfully created.".format(
                                   server_name))
    elif '1' in str(result):
        check_date = "SELECT * FROM `{}` WHERE date = '{}'".format(server_name, date)
        cur.execute(check_date)
        date_result = cur.fetchall()
        if date_result:
            #  checking class
            return await bot.safe_send_message(channel, "No worries, this server was already backed up today!")