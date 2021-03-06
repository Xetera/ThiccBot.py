import asyncio
import inspect
import os
import shlex
import shutil
import sys
import time
import traceback
from collections import defaultdict
from datetime import timedelta
from functools import wraps
from io import BytesIO
from random import choice, shuffle
from textwrap import dedent

import aiohttp
import discord
import pymysql
from discord import utils
from discord.enums import ChannelType
from discord.ext.commands.bot import _get_variable
from discord.object import Object
from discord.voice_client import VoiceClient

from musicbot.config import Config, ConfigDefaults
from musicbot.lib.srv import ThreadedServer
from musicbot.permissions import Permissions, PermissionsDefaults
from musicbot.player import MusicPlayer
from musicbot.playlist import Playlist
from musicbot.utils import load_file, write_file, sane_round_int

from . import downloader
from . import exceptions
from . import memes

from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH
from .constants import VERSION as BOTVERSION
from .opus_loader import load_opus_lib

from .secret import *
from .lib.srv import ThreadedServer

load_opus_lib()


class SkipState:
	def __init__(self):
		self.skippers = set()
		self.skip_msgs = set()

	@property
	def skip_count(self):
		return len(self.skippers)

	def reset(self):
		self.skippers.clear()
		self.skip_msgs.clear()

	def add_skipper(self, skipper, msg):
		self.skippers.add(skipper)
		self.skip_msgs.add(msg)
		return self.skip_count


class Response:
	def __init__(self, content, reply=False, delete_after=0):
		self.content = content
		self.reply = reply
		self.delete_after = delete_after


class MusicBot(discord.Client):

	def __init__(self, config_file=ConfigDefaults.options_file, perms_file=PermissionsDefaults.perms_file):
		self.dirname = os.path.dirname(__file__).rsplit('\\', 1)[0]
		self.players = {}
		self.the_voice_clients = {}
		self.locks = defaultdict(asyncio.Lock)
		self.voice_client_connect_lock = asyncio.Lock()
		self.voice_client_move_lock = asyncio.Lock()

		self.config = Config(config_file)
		self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])

		self.blacklist = set(load_file(self.config.blacklist_file))
		self.autoplaylist = load_file(self.config.auto_playlist_file)
		self.downloader = downloader.Downloader(download_folder='audio_cache')

		self.exit_signal = None
		self.init_ok = False
		self.cached_client_id = None

		if not self.autoplaylist:
			print("Warning: Autoplaylist is empty, disabling.")
			self.config.auto_playlist = False

		# TODO: Do these properly
		ssd_defaults = {'last_np_msg': None, 'auto_paused': False}
		self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))

		super().__init__()
		self.aiosession = aiohttp.ClientSession(loop=self.loop)
		self.http.user_agent += ' MusicBot/%s' % BOTVERSION

	# TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
	def owner_only(func):
		@wraps(func)
		async def wrapper(self, *args, **kwargs):
			# Only allow the owner to use these commands
			orig_msg = _get_variable('message')

			if not orig_msg or orig_msg.author.id == self.config.owner_id:
				return await func(self, *args, **kwargs)
			else:
				raise exceptions.PermissionsError("only the owner can use this command", expire_in=30)

		return wrapper

	@staticmethod
	def _fixg(x, dp=2):
		return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')

	def _get_owner(self, voice=False):
		if voice:
			for server in self.servers:
				for channel in server.channels:
					for m in channel.voice_members:
						if m.id == self.config.owner_id:
							return m
		else:
			return discord.utils.find(lambda m: m.id == self.config.owner_id, self.get_all_members())

	def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
		try:
			shutil.rmtree(path)
			return True
		except:
			try:
				os.rename(path, path + '__')
			except:
				return False
			try:
				shutil.rmtree(path)
			except:
				os.rename(path + '__', path)
				return False

		return True

	# TODO: autosummon option to a specific channel
	async def _auto_summon(self):
		owner = self._get_owner(voice=True)
		if owner:
			self.safe_print("Found owner in \"%s\", attempting to join..." % owner.voice_channel.name)
			# TODO: Effort
			await self.cmd_summon(owner.voice_channel, owner, None)
			return owner.voice_channel

	async def _autojoin_channels(self, channels):
		joined_servers = []

		for channel in channels:
			if channel.server in joined_servers:
				print("Already joined a channel in %s, skipping" % channel.server.name)
				continue

			if channel and channel.type == discord.ChannelType.voice:
				self.safe_print("Attempting to autojoin %s in %s" % (channel.name, channel.server.name))

				chperms = channel.permissions_for(channel.server.me)

				if not chperms.connect:
					self.safe_print("Cannot join channel \"%s\", no permission." % channel.name)
					continue

				elif not chperms.speak:
					self.safe_print("Will not join channel \"%s\", no permission to speak." % channel.name)
					continue

				try:
					player = await self.get_player(channel, create=True)

					if player.is_stopped:
						player.play()

					if self.config.auto_playlist:
						await self.on_player_finished_playing(player)

					joined_servers.append(channel.server)
				except Exception as e:
					if self.config.debug_mode:
						traceback.print_exc()
					print("Failed to join", channel.name)

			elif channel:
				print("Not joining %s on %s, that's a text channel." % (channel.name, channel.server.name))

			else:
				print("Invalid channel thing: " + channel)

	async def _wait_delete_msg(self, message, after):
		await asyncio.sleep(after)
		await self.safe_delete_message(message)

	# TODO: Check to see if I can just move this to on_message after the response check
	async def _manual_delete_check(self, message, *, quiet=False):
		if self.config.delete_invoking:
			await self.safe_delete_message(message, quiet=quiet)

	async def _check_ignore_non_voice(self, msg):
		vc = msg.server.me.voice_channel

		# If we've connected to a voice chat and we're in the same voice channel
		if not vc or vc == msg.author.voice_channel:
			return True
		else:
			raise exceptions.PermissionsError(
				"you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

	async def generate_invite_link(self, *, permissions=None, server=None):
		if not self.cached_client_id:
			appinfo = await self.application_info()
			self.cached_client_id = appinfo.id

		return discord.utils.oauth_url(self.cached_client_id, permissions=permissions, server=server)

	async def get_voice_client(self, channel):
		if isinstance(channel, Object):
			channel = self.get_channel(channel.id)

		if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
			raise AttributeError('Channel passed must be a voice channel')

		with await self.voice_client_connect_lock:
			server = channel.server
			if server.id in self.the_voice_clients:
				return self.the_voice_clients[server.id]

			s_id = self.ws.wait_for('VOICE_STATE_UPDATE', lambda d: d.get('user_id') == self.user.id)
			_voice_data = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

			await self.ws.voice_state(server.id, channel.id)

			s_id_data = await asyncio.wait_for(s_id, timeout=10, loop=self.loop)
			voice_data = await asyncio.wait_for(_voice_data, timeout=10, loop=self.loop)
			session_id = s_id_data.get('session_id')

			kwargs = {
				'user': self.user,
				'channel': channel,
				'data': voice_data,
				'loop': self.loop,
				'session_id': session_id,
				'main_ws': self.ws
			}
			voice_client = VoiceClient(**kwargs)
			self.the_voice_clients[server.id] = voice_client

			retries = 3
			for x in range(retries):
				try:
					print("Attempting connection...")
					await asyncio.wait_for(voice_client.connect(), timeout=10, loop=self.loop)
					print("Connection established.")
					break
				except:
					traceback.print_exc()
					print("Failed to connect, retrying (%s/%s)..." % (x+1, retries))
					await asyncio.sleep(1)
					await self.ws.voice_state(server.id, None, self_mute=True)
					await asyncio.sleep(1)

					if x == retries-1:
						raise exceptions.HelpfulError(
							"Cannot establish connection to voice chat.  "
							"Something may be blocking outgoing UDP connections.",

							"This may be an issue with a firewall blocking UDP.  "
							"Figure out what is blocking UDP and disable it.  "
							"It's most likely a system firewall or overbearing anti-virus firewall.  "
						)

			return voice_client

	async def mute_voice_client(self, channel, mute):
		await self._update_voice_state(channel, mute=mute)

	async def deafen_voice_client(self, channel, deaf):
		await self._update_voice_state(channel, deaf=deaf)

	async def move_voice_client(self, channel):
		await self._update_voice_state(channel)

	async def reconnect_voice_client(self, server):
		if server.id not in self.the_voice_clients:
			return

		vc = self.the_voice_clients.pop(server.id)
		_paused = False

		player = None
		if server.id in self.players:
			player = self.players[server.id]
			if player.is_playing:
				player.pause()
				_paused = True

		try:
			await vc.disconnect()
		except:
			print("Error disconnecting during reconnect")
			traceback.print_exc()

		await asyncio.sleep(0.1)

		if player:
			new_vc = await self.get_voice_client(vc.channel)
			player.reload_voice(new_vc)

			if player.is_paused and _paused:
				player.resume()

	async def disconnect_voice_client(self, server):
		if server.id not in self.the_voice_clients:
			return

		if server.id in self.players:
			self.players.pop(server.id).kill()

		await self.the_voice_clients.pop(server.id).disconnect()

	async def disconnect_all_voice_clients(self):
		for vc in self.the_voice_clients.copy().values():
			await self.disconnect_voice_client(vc.channel.server)

	async def _update_voice_state(self, channel, *, mute=False, deaf=False):
		if isinstance(channel, Object):
			channel = self.get_channel(channel.id)

		if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
			raise AttributeError('Channel passed must be a voice channel')

		# I'm not sure if this lock is actually needed
		with await self.voice_client_move_lock:
			server = channel.server

			payload = {
				'op': 4,
				'd': {
					'guild_id': server.id,
					'channel_id': channel.id,
					'self_mute': mute,
					'self_deaf': deaf
				}
			}

			await self.ws.send(utils.to_json(payload))
			self.the_voice_clients[server.id].channel = channel

	async def get_player(self, channel, create=False) -> MusicPlayer:
		server = channel.server

		if server.id not in self.players:
			if not create:
				raise exceptions.CommandError(
					'The bot is not in a voice channel.  '
					'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

			voice_client = await self.get_voice_client(channel)

			playlist = Playlist(self)
			player = MusicPlayer(self, voice_client, playlist) \
				.on('play', self.on_player_play) \
				.on('resume', self.on_player_resume) \
				.on('pause', self.on_player_pause) \
				.on('stop', self.on_player_stop) \
				.on('finished-playing', self.on_player_finished_playing) \
				.on('entry-added', self.on_player_entry_added)

			player.skip_state = SkipState()
			self.players[server.id] = player

		return self.players[server.id]

	async def on_player_play(self, player, entry):
		await self.update_now_playing(entry)
		player.skip_state.reset()

		channel = entry.meta.get('channel', None)
		author = entry.meta.get('author', None)

		if channel and author:
			last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
			if last_np_msg and last_np_msg.channel == channel:

				async for lmsg in self.logs_from(channel, limit=1):
					if lmsg != last_np_msg and last_np_msg:
						await self.safe_delete_message(last_np_msg)
						self.server_specific_data[channel.server]['last_np_msg'] = None
					break  # This is probably redundant

			if self.config.now_playing_mentions:
				newmsg = '%s - your song **%s** is now playing in %s!' % (
					entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
			else:
				newmsg = 'Now playing in %s: **%s**' % (
					player.voice_client.channel.name, entry.title)

			if self.server_specific_data[channel.server]['last_np_msg']:
				self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
			else:
				self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

	async def on_player_resume(self, entry, **_):
		await self.update_now_playing(entry)

	async def on_player_pause(self, entry, **_):
		await self.update_now_playing(entry, True)

	async def on_player_stop(self, **_):
		await self.update_now_playing()

	async def on_player_finished_playing(self, player, **_):
		if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
			while self.autoplaylist:
				song_url = choice(self.autoplaylist)
				info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)

				if not info:
					self.autoplaylist.remove(song_url)
					self.safe_print("[Info] Removing unplayable song from autoplaylist: %s" % song_url)
					write_file(self.config.auto_playlist_file, self.autoplaylist)
					continue

				if info.get('entries', None):  # or .get('_type', '') == 'playlist'
					pass  # Wooo playlist
					# Blarg how do I want to do this

				# TODO: better checks here
				try:
					await player.playlist.add_entry(song_url, channel=None, author=None)
				except exceptions.ExtractionError as e:
					print("Error adding song from autoplaylist:", e)
					continue

				break

			if not self.autoplaylist:
				print("[Warning] No playable songs in the autoplaylist, disabling.")
				self.config.auto_playlist = False

	async def on_player_entry_added(self, playlist, entry, **_):
		pass

	async def update_now_playing(self, entry=None, is_paused=False):
		game = None

		if self.user.bot:
			activeplayers = sum(1 for p in self.players.values() if p.is_playing)
			if activeplayers > 1:
				game = discord.Game(name="music on %s servers" % activeplayers)
				entry = None

			elif activeplayers == 1:
				player = discord.utils.get(self.players.values(), is_playing=True)
				entry = player.current_entry

		if entry:
			prefix = u'\u275A\u275A ' if is_paused else ''

			name = u'{}{}'.format(prefix, entry.title)[:128]
			game = discord.Game(name=name)

		await self.change_status(game)

	async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
		msg = None
		try:
			msg = await self.send_message(dest, content, tts=tts)

			if msg and expire_in:
				asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

			if also_delete and isinstance(also_delete, discord.Message):
				asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

		except discord.Forbidden:
			if not quiet:
				self.safe_print("Warning: Cannot send message to %s, no permission" % dest.name)

		except discord.NotFound:
			if not quiet:
				self.safe_print("Warning: Cannot send message to %s, invalid channel?" % dest.name)

		return msg

	async def safe_delete_message(self, message, *, quiet=False):
		try:
			return await self.delete_message(message)

		except discord.Forbidden:
			if not quiet:
				self.safe_print("Warning: Cannot delete message \"%s\", no permission" % message.clean_content)

		except discord.NotFound:
			if not quiet:
				self.safe_print("Warning: Cannot delete message \"%s\", message not found" % message.clean_content)

	async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
		try:
			return await self.edit_message(message, new)

		except discord.NotFound:
			if not quiet:
				self.safe_print("Warning: Cannot edit message \"%s\", message not found" % message.clean_content)
			if send_if_fail:
				if not quiet:
					print("Sending instead")
				return await self.safe_send_message(message.channel, new)

	def safe_print(self, content, *, end='\n', flush=True):
		sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
		if flush: sys.stdout.flush()

	async def send_typing(self, destination):
		try:
			return await super().send_typing(destination)
		except discord.Forbidden:
			if self.config.debug_mode:
				print("Could not send typing to %s, no permssion" % destination)

	async def edit_profile(self, **fields):
		if self.user.bot:
			return await super().edit_profile(**fields)
		else:
			return await super().edit_profile(self.config._password,**fields)

	def _cleanup(self):
		try:
			self.loop.run_until_complete(self.logout())
		except: # Can be ignored
			pass

		pending = asyncio.Task.all_tasks()
		gathered = asyncio.gather(*pending)

		try:
			gathered.cancel()
			self.loop.run_until_complete(gathered)
			gathered.exception()
		except: # Can be ignored
			pass

	# noinspection PyMethodOverriding
	def run(self):
		try:
			self.loop.run_until_complete(self.start(*self.config.auth))

		except discord.errors.LoginFailure:
			# Add if token, else
			raise exceptions.HelpfulError(
				"Bot cannot login, bad credentials.",
				"Fix your Email or Password or Token in the options file.  "
				"Remember that each field should be on their own line.")

		finally:
			try:
				self._cleanup()
			except Exception as e:
				print("Error in cleanup:", e)

			self.loop.close()
			if self.exit_signal:
				raise self.exit_signal

	async def logout(self):
		await self.disconnect_all_voice_clients()
		return await super().logout()

	async def on_error(self, event, *args, **kwargs):
		ex_type, ex, stack = sys.exc_info()

		if ex_type == exceptions.HelpfulError:
			print("Exception in", event)
			print(ex.message)

			await asyncio.sleep(2)  # don't ask
			await self.logout()

		elif issubclass(ex_type, exceptions.Signal):
			self.exit_signal = ex_type
			await self.logout()

		else:
			traceback.print_exc()

	async def on_resumed(self):
		for vc in self.the_voice_clients.values():
			vc.main_ws = self.ws

	async def on_ready(self):
		self.loop.create_task(self.whatsapp())

		print('\rConnected!  Musicbot v%s\n' % BOTVERSION)

		if self.config.owner_id == self.user.id:
			raise exceptions.HelpfulError(
				"Your OwnerID is incorrect or you've used the wrong credentials.",

				"The bot needs its own account to function.  "
				"The OwnerID is the id of the owner, not the bot.  "
				"Figure out which one is which and use the correct information.")

		self.init_ok = True

		self.safe_print("Bot:   %s/%s#%s" % (self.user.id, self.user.name, self.user.discriminator))

		owner = self._get_owner(voice=True) or self._get_owner()
		if owner and self.servers:
			self.safe_print("Owner: %s/%s#%s\n" % (owner.id, owner.name, owner.discriminator))

			print('Server List:')
			[self.safe_print(' - ' + s.name) for s in self.servers]

		elif self.servers:
			print("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

			print('Server List:')
			[self.safe_print(' - ' + s.name) for s in self.servers]

		else:
			print("Owner unknown, bot is not on any servers.")
			if self.user.bot:
				print("\nTo make the bot join a server, paste this link in your browser.")
				print("Note: You should be logged into your main account and have \n"
					  "manage server permissions on the server you want the bot to join.\n")
				print("    " + await self.generate_invite_link())

		print()

		if self.config.bound_channels:
			chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
			chlist.discard(None)
			invalids = set()

			invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)
			chlist.difference_update(invalids)
			self.config.bound_channels.difference_update(invalids)

			print("Bound to text channels:")
			[self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

			if invalids and self.config.debug_mode:
				print("\nNot binding to voice channels:")
				[self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

			print()

		else:
			print("Not bound to any text channels")

		if self.config.autojoin_channels:
			chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
			chlist.discard(None)
			invalids = set()

			invalids.update(c for c in chlist if c.type == discord.ChannelType.text)
			chlist.difference_update(invalids)
			self.config.autojoin_channels.difference_update(invalids)

			print("Autojoining voice chanels:")
			[self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

			if invalids and self.config.debug_mode:
				print("\nCannot join text channels:")
				[self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

			autojoin_channels = chlist

		else:
			print("Not autojoining any voice channels")
			autojoin_channels = set()

		print()
		print("Options:")

		self.safe_print("  Command prefix: " + self.config.command_prefix)
		print("  Default volume: %s%%" % int(self.config.default_volume * 100))
		print("  Skip threshold: %s votes or %s%%" % (
			self.config.skips_required, self._fixg(self.config.skip_ratio_required * 100)))
		print("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
		print("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
		print("  Auto-Playlist: " + ['Disabled', 'Enabled'][self.config.auto_playlist])
		print("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
		print("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
		if self.config.delete_messages:
			print("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
		print("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
		print("  Downloaded songs will be %s" % ['deleted', 'saved'][self.config.save_videos])
		print()

		# maybe option to leave the ownerid blank and generate a random command for the owner to use
		# wait_for_message is pretty neato

		if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
			if self._delete_old_audiocache():
				print("Deleting old audio cache")
			else:
				print("Could not delete old audio cache, moving on.")

		if self.config.autojoin_channels:
			await self._autojoin_channels(autojoin_channels)

		elif self.config.auto_summon:
			print("Attempting to autosummon...", flush=True)

			# waitfor + get value
			owner_vc = await self._auto_summon()

			if owner_vc:
				print("Done!", flush=True)  # TODO: Change this to "Joined server/channel"
				if self.config.auto_playlist:
					print("Starting auto-playlist")
					await self.on_player_finished_playing(await self.get_player(owner_vc))
			else:
				print("Owner not found in a voice channel, could not autosummon.")

		print()
		# t-t-th-th-that's all folks!

	async def cmd_help(self, command=None):
		"""
		Usage:
			{command_prefix}help [command]

		Prints a help message.
		If a command is specified, it prints a help message for that command.
		Otherwise, it lists the available commands.
		"""

		if command:
			cmd = getattr(self, 'cmd_' + command, None)
			if cmd:
				return Response(
					"```\n{}```".format(
						dedent(cmd.__doc__),
						command_prefix=self.config.command_prefix
					),
					delete_after=60
				)
			else:
				return Response("No such command", delete_after=10)

		else:
			helpmsg = "**Commands**\n```"
			commands = []

			for att in dir(self):
				if att.startswith('cmd_') and att != 'cmd_help':
					command_name = att.replace('cmd_', '').lower()
					commands.append("{}{}".format(self.config.command_prefix, command_name))

			helpmsg += ", ".join(commands)
			helpmsg += "```"
			helpmsg += "https://github.com/SexualRhinoceros/MusicBot/wiki/Commands-list"

			return Response(helpmsg, reply=True, delete_after=60)

	async def cmd_blacklist(self, message, user_mentions, option, something):
		"""
		Usage:
			{command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

		Add or remove users to the blacklist.
		Blacklisted users are forbidden from using bot commands.
		"""

		if not user_mentions:
			raise exceptions.CommandError("No users listed.", expire_in=20)

		if option not in ['+', '-', 'add', 'remove']:
			raise exceptions.CommandError(
				'Invalid option "%s" specified, use +, -, add, or remove' % option, expire_in=20
			)

		for user in user_mentions.copy():
			if user.id == self.config.owner_id:
				print("[Commands:Blacklist] The owner cannot be blacklisted.")
				user_mentions.remove(user)

		old_len = len(self.blacklist)

		if option in ['+', 'add']:
			self.blacklist.update(user.id for user in user_mentions)

			write_file(self.config.blacklist_file, self.blacklist)

			return Response(
				'%s users have been added to the blacklist' % (len(self.blacklist) - old_len),
				reply=True, delete_after=10
			)

		else:
			if self.blacklist.isdisjoint(user.id for user in user_mentions):
				return Response('none of those users are in the blacklist.', reply=True, delete_after=10)

			else:
				self.blacklist.difference_update(user.id for user in user_mentions)
				write_file(self.config.blacklist_file, self.blacklist)

				return Response(
					'%s users have been removed from the blacklist' % (old_len - len(self.blacklist)),
					reply=True, delete_after=10
				)

	async def cmd_id(self, author, user_mentions):
		"""
		Usage:
			{command_prefix}id [@user]

		Tells the user their id or the id of another user.
		"""
		if not user_mentions:
			return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
		else:
			usr = user_mentions[0]
			return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

	@owner_only
	async def cmd_joinserver(self, message, server_link=None):
		"""
		Usage:
			{command_prefix}joinserver invite_link

		Asks the bot to join a server.  Note: Bot accounts cannot use invite links.
		"""

		if self.user.bot:
			url = await self.generate_invite_link()
			return Response(
				"Bot accounts can't use invite links!  Click here to invite me: \n{}".format(url),
				reply=True, delete_after=30
			)

		try:
			if server_link:
				await self.accept_invite(server_link)
				return Response(":+1:")

		except:
			raise exceptions.CommandError('Invalid URL provided:\n{}\n'.format(server_link), expire_in=30)

	async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
		"""
		Usage:
			{command_prefix}play song_link
			{command_prefix}play text to search for

		Adds the song to the playlist.  If a link is not provided, the first
		result from a youtube search is added to the queue.
		"""

		song_url = song_url.strip('<>')

		if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
			raise exceptions.PermissionsError(
				"You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
			)

		await self.send_typing(channel)

		if leftover_args:
			song_url = ' '.join([song_url, *leftover_args])

		try:
			info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
		except Exception as e:
			raise exceptions.CommandError(e, expire_in=30)

		if not info:
			raise exceptions.CommandError("That video cannot be played.", expire_in=30)

		# abstract the search handling away from the user
		# our ytdl options allow us to use search strings as input urls
		if info.get('url', '').startswith('ytsearch'):
			# print("[Command:play] Searching for \"%s\"" % song_url)
			info = await self.downloader.extract_info(
				player.playlist.loop,
				song_url,
				download=False,
				process=True,    # ASYNC LAMBDAS WHEN
				on_error=lambda e: asyncio.ensure_future(
					self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
				retry_on_error=True
			)

			if not info:
				raise exceptions.CommandError(
					"Error extracting info from search string, youtubedl returned no data.  "
					"You may need to restart the bot if this continues to happen.", expire_in=30
				)

			if not all(info.get('entries', [])):
				# empty list, no data
				return

			song_url = info['entries'][0]['webpage_url']
			info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
			# Now I could just do: return await self.cmd_play(player, channel, author, song_url)
			# But this is probably fine

		# TODO: Possibly add another check here to see about things like the bandcamp issue
		# TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

		if 'entries' in info:
			# I have to do exe extra checks anyways because you can request an arbitrary number of search results
			if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
				raise exceptions.PermissionsError("You are not allowed to request playlists", expire_in=30)

			# The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
			num_songs = sum(1 for _ in info['entries'])

			if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
				raise exceptions.PermissionsError(
					"Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
					expire_in=30
				)

			# This is a little bit weird when it says (x + 0 > y), I might add the other check back in
			if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
				raise exceptions.PermissionsError(
					"Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
						num_songs, player.playlist.count_for_user(author), permissions.max_songs),
					expire_in=30
				)

			if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
				try:
					return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
				except exceptions.CommandError:
					raise
				except Exception as e:
					traceback.print_exc()
					raise exceptions.CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

			t0 = time.time()

			# My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
			# monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
			# I don't think we can hook into it anyways, so this will have to do.
			# It would probably be a thread to check a few playlists and get the speed from that
			# Different playlists might download at different speeds though
			wait_per_song = 1.2

			procmesg = await self.safe_send_message(
				channel,
				'Gathering playlist information for {} songs{}'.format(
					num_songs,
					', ETA: {} seconds'.format(self._fixg(
						num_songs * wait_per_song)) if num_songs >= 10 else '.'))

			# We don't have a pretty way of doing this yet.  We need either a loop
			# that sends these every 10 seconds or a nice context manager.
			await self.send_typing(channel)

			# TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
			#       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

			entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

			tnow = time.time()
			ttime = tnow - t0
			listlen = len(entry_list)
			drop_count = 0

			if permissions.max_song_length:
				for e in entry_list.copy():
					if e.duration > permissions.max_song_length:
						player.playlist.entries.remove(e)
						entry_list.remove(e)
						drop_count += 1
						# Im pretty sure there's no situation where this would ever break
						# Unless the first entry starts being played, which would make this a race condition
				if drop_count:
					print("Dropped %s songs" % drop_count)

			print("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
				listlen,
				self._fixg(ttime),
				ttime / listlen,
				ttime / listlen - wait_per_song,
				self._fixg(wait_per_song * num_songs))
			)

			await self.safe_delete_message(procmesg)

			if not listlen - drop_count:
				raise exceptions.CommandError(
					"No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
					expire_in=30
				)

			reply_text = "Enqueued **%s** songs to be played. Position in queue: %s"
			btext = str(listlen - drop_count)

		else:
			if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
				raise exceptions.PermissionsError(
					"Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
					expire_in=30
				)

			try:
				entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

			except exceptions.WrongEntryTypeError as e:
				if e.use_url == song_url:
					print("[Warning] Determined incorrect entry type, but suggested url is the same.  Help.")

				if self.config.debug_mode:
					print("[Info] Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
					print("[Info] Using \"%s\" instead" % e.use_url)

				return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

			reply_text = "Enqueued **%s** to be played. Position in queue: %s"
			btext = entry.title

		if position == 1 and player.is_stopped:
			position = 'Up next!'
			reply_text %= (btext, position)

		else:
			try:
				time_until = await player.playlist.estimate_time_until(position, player)
				reply_text += ' - estimated time until playing: %s'
			except:
				traceback.print_exc()
				time_until = ''

			reply_text %= (btext, position, time_until)

		return Response(reply_text, delete_after=30)

	async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
		"""
		Secret handler to use the async wizardry to make playlist queuing non-"blocking"
		"""

		await self.send_typing(channel)
		info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

		if not info:
			raise exceptions.CommandError("That playlist cannot be played.")

		num_songs = sum(1 for _ in info['entries'])
		t0 = time.time()

		busymsg = await self.safe_send_message(
			channel, "Processing %s songs..." % num_songs)  # TODO: From playlist_title
		await self.send_typing(channel)

		entries_added = 0
		if extractor_type == 'youtube:playlist':
			try:
				entries_added = await player.playlist.async_process_youtube_playlist(
					playlist_url, channel=channel, author=author)
				# TODO: Add hook to be called after each song
				# TODO: Add permissions

			except Exception:
				traceback.print_exc()
				raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

		elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
			try:
				entries_added = await player.playlist.async_process_sc_bc_playlist(
					playlist_url, channel=channel, author=author)
				# TODO: Add hook to be called after each song
				# TODO: Add permissions

			except Exception:
				traceback.print_exc()
				raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)


		songs_processed = len(entries_added)
		drop_count = 0
		skipped = False

		if permissions.max_song_length:
			for e in entries_added.copy():
				if e.duration > permissions.max_song_length:
					try:
						player.playlist.entries.remove(e)
						entries_added.remove(e)
						drop_count += 1
					except:
						pass

			if drop_count:
				print("Dropped %s songs" % drop_count)

			if player.current_entry and player.current_entry.duration > permissions.max_song_length:
				await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
				self.server_specific_data[channel.server]['last_np_msg'] = None
				skipped = True
				player.skip()
				entries_added.pop()

		await self.safe_delete_message(busymsg)

		songs_added = len(entries_added)
		tnow = time.time()
		ttime = tnow - t0
		wait_per_song = 1.2
		# TODO: actually calculate wait per song in the process function and return that too

		# This is technically inaccurate since bad songs are ignored but still take up time
		print("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
			songs_processed,
			num_songs,
			self._fixg(ttime),
			ttime / num_songs,
			ttime / num_songs - wait_per_song,
			self._fixg(wait_per_song * num_songs))
		)

		if not songs_added:
			basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
			if skipped:
				basetext += "\nAdditionally, the current song was skipped for being too long."

			raise exceptions.CommandError(basetext, expire_in=30)

		return Response("Enqueued {} songs to be played in {} seconds".format(
			songs_added, self._fixg(ttime, 1)), delete_after=30)

	async def cmd_search(self, player, channel, author, permissions, leftover_args):
		"""
		Usage:
			{command_prefix}search [service] [number] query

		Searches a service for a video and adds it to the queue.
		- service: any one of the following services:
			- youtube (yt) (default if unspecified)
			- soundcloud (sc)
			- yahoo (yh)
		- number: return a number of video results and waits for user to choose one
		  - defaults to 1 if unspecified
		  - note: If your search query starts with a number,
				  you must put your query in quotes
			- ex: {command_prefix}search 2 "I ran seagulls"
		"""

		if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
			raise exceptions.PermissionsError(
				"You have reached your playlist item limit (%s)" % permissions.max_songs,
				expire_in=30
			)

		def argcheck():
			if not leftover_args:
				raise exceptions.CommandError(
					"Please specify a search query.\n%s" % dedent(
						self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
					expire_in=60
				)

		argcheck()

		try:
			leftover_args = shlex.split(' '.join(leftover_args))
		except ValueError:
			raise exceptions.CommandError("Please quote your search query properly.", expire_in=30)

		service = 'youtube'
		items_requested = 3
		max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
		services = {
			'youtube': 'ytsearch',
			'soundcloud': 'scsearch',
			'yahoo': 'yvsearch',
			'yt': 'ytsearch',
			'sc': 'scsearch',
			'yh': 'yvsearch'
		}

		if leftover_args[0] in services:
			service = leftover_args.pop(0)
			argcheck()

		if leftover_args[0].isdigit():
			items_requested = int(leftover_args.pop(0))
			argcheck()

			if items_requested > max_items:
				raise exceptions.CommandError("You cannot search for more than %s videos" % max_items)

		# Look jake, if you see this and go "what the fuck are you doing"
		# and have a better idea on how to do this, i'd be delighted to know.
		# I don't want to just do ' '.join(leftover_args).strip("\"'")
		# Because that eats both quotes if they're there
		# where I only want to eat the outermost ones
		if leftover_args[0][0] in '\'"':
			lchar = leftover_args[0][0]
			leftover_args[0] = leftover_args[0].lstrip(lchar)
			leftover_args[-1] = leftover_args[-1].rstrip(lchar)

		search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

		search_msg = await self.send_message(channel, "Searching for videos...")
		await self.send_typing(channel)

		try:
			info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

		except Exception as e:
			await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
			return
		else:
			await self.safe_delete_message(search_msg)

		if not info:
			return Response("No videos found.", delete_after=30)

		def check(m):
			return (
				m.content.lower()[0] in 'yn' or
				# hardcoded function name weeee
				m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
				m.content.lower().startswith('exit'))

		for e in info['entries']:
			result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
				info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

			confirm_message = await self.safe_send_message(channel, "Is this ok? Type `y`, `n` or `exit`")
			response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

			if not response_message:
				await self.safe_delete_message(result_message)
				await self.safe_delete_message(confirm_message)
				return Response("Ok nevermind.", delete_after=30)

			# They started a new search query so lets clean up and bugger off
			elif response_message.content.startswith(self.config.command_prefix) or \
					response_message.content.lower().startswith('exit'):

				await self.safe_delete_message(result_message)
				await self.safe_delete_message(confirm_message)
				return

			if response_message.content.lower().startswith('y'):
				await self.safe_delete_message(result_message)
				await self.safe_delete_message(confirm_message)
				await self.safe_delete_message(response_message)

				await self.cmd_play(player, channel, author, permissions, [], e['webpage_url'])

				return Response("Alright, coming right up!", delete_after=30)
			else:
				await self.safe_delete_message(result_message)
				await self.safe_delete_message(confirm_message)
				await self.safe_delete_message(response_message)

		return Response("Oh well :frowning:", delete_after=30)

	async def cmd_np(self, player, channel, server, message):
		"""
		Usage:
			{command_prefix}np

		Displays the current song in chat.
		"""

		if player.current_entry:
			if self.server_specific_data[server]['last_np_msg']:
				await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
				self.server_specific_data[server]['last_np_msg'] = None

			song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
			song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
			prog_str = '`[%s/%s]`' % (song_progress, song_total)

			if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
				np_text = "Now Playing: **%s** added by **%s** %s\n" % (
					player.current_entry.title, player.current_entry.meta['author'].name, prog_str)
			else:
				np_text = "Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str)

			self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
			await self._manual_delete_check(message)
		else:
			return Response(
				'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix),
				delete_after=30
			)

	async def cmd_summon(self, channel, author, voice_channel):
		"""
		Usage:
			{command_prefix}summon

		Call the bot to the summoner's voice channel.
		"""

		if not author.voice_channel:
			raise exceptions.CommandError('You are not in a voice channel!')

		voice_client = self.the_voice_clients.get(channel.server.id, None)
		if voice_client and voice_client.channel.server == author.voice_channel.server:
			await self.move_voice_client(author.voice_channel)
			return

		# move to _verify_vc_perms?
		chperms = author.voice_channel.permissions_for(author.voice_channel.server.me)

		if not chperms.connect:
			self.safe_print("Cannot join channel \"%s\", no permission." % author.voice_channel.name)
			return Response(
				"```Cannot join channel \"%s\", no permission.```" % author.voice_channel.name,
				delete_after=25
			)

		elif not chperms.speak:
			self.safe_print("Will not join channel \"%s\", no permission to speak." % author.voice_channel.name)
			return Response(
				"```Will not join channel \"%s\", no permission to speak.```" % author.voice_channel.name,
				delete_after=25
			)

		player = await self.get_player(author.voice_channel, create=True)

		if player.is_stopped:
			player.play()

		if self.config.auto_playlist:
			await self.on_player_finished_playing(player)

	async def cmd_pause(self, player):
		"""
		Usage:
			{command_prefix}pause

		Pauses playback of the current song.
		"""

		if player.is_playing:
			player.pause()

		else:
			raise exceptions.CommandError('Player is not playing.', expire_in=30)

	async def cmd_resume(self, player):
		"""
		Usage:
			{command_prefix}resume

		Resumes playback of a paused song.
		"""

		if player.is_paused:
			player.resume()

		else:
			raise exceptions.CommandError('Player is not paused.', expire_in=30)

	async def cmd_shuffle(self, channel, player):
		"""
		Usage:
			{command_prefix}shuffle

		Shuffles the playlist.
		"""

		player.playlist.shuffle()

		cards = [':spades:',':clubs:',':hearts:',':diamonds:']
		hand = await self.send_message(channel, ' '.join(cards))
		await asyncio.sleep(0.6)

		for x in range(4):
			shuffle(cards)
			await self.safe_edit_message(hand, ' '.join(cards))
			await asyncio.sleep(0.6)

		await self.safe_delete_message(hand, quiet=True)
		return Response(":ok_hand:", delete_after=15)

	async def cmd_clear(self, player, author):
		"""
		Usage:
			{command_prefix}clear

		Clears the playlist.
		"""

		player.playlist.clear()
		return Response(':put_litter_in_its_place:', delete_after=20)

	async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
		"""
		Usage:
			{command_prefix}skip

		Skips the current song when enough votes are cast, or by the bot owner.
		"""

		if player.is_stopped:
			raise exceptions.CommandError("Can't skip! The player is not playing!", expire_in=20)

		if not player.current_entry:
			if player.playlist.peek():
				if player.playlist.peek()._is_downloading:
					# print(player.playlist.peek()._waiting_futures[0].__dict__)
					return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

				elif player.playlist.peek().is_downloaded:
					print("The next song will be played shortly.  Please wait.")
				else:
					print("Something odd is happening.  "
						  "You might want to restart the bot if it doesn't start working.")
			else:
				print("Something strange is happening.  "
					  "You might want to restart the bot if it doesn't start working.")

		if author.id == self.config.owner_id \
				or permissions.instaskip \
				or author == player.current_entry.meta.get('author', None):

			player.skip()  # check autopause stuff here
			await self._manual_delete_check(message)
			return

		# TODO: ignore person if they're deaf or take them out of the list or something?
		# Currently is recounted if they vote, deafen, then vote

		num_voice = sum(1 for m in voice_channel.voice_members if not (
			m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

		num_skips = player.skip_state.add_skipper(author.id, message)

		skips_remaining = min(self.config.skips_required,
							  sane_round_int(num_voice * self.config.skip_ratio_required)) - num_skips

		if skips_remaining <= 0:
			player.skip()  # check autopause stuff here
			return Response(
				'your skip for **{}** was acknowledged.'
				'\nThe vote to skip has been passed.{}'.format(
					player.current_entry.title,
					' Next song coming up!' if player.playlist.peek() else ''
				),
				reply=True,
				delete_after=20
			)

		else:
			# TODO: When a song gets skipped, delete the old x needed to skip messages
			return Response(
				'your skip for **{}** was acknowledged.'
				'\n**{}** more {} required to vote to skip this song.'.format(
					player.current_entry.title,
					skips_remaining,
					'person is' if skips_remaining == 1 else 'people are'
				),
				reply=True,
				delete_after=20
			)

	async def cmd_volume(self, message, player, new_volume=None):
		"""
		Usage:
			{command_prefix}volume (+/-)[volume]

		Sets the playback volume. Accepted values are from 1 to 100.
		Putting + or - before the volume will make the volume change relative to the current volume.
		"""

		if not new_volume:
			return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

		relative = False
		if new_volume[0] in '+-':
			relative = True

		try:
			new_volume = int(new_volume)

		except ValueError:
			raise exceptions.CommandError('{} is not a valid number'.format(new_volume), expire_in=20)

		if relative:
			vol_change = new_volume
			new_volume += (player.volume * 100)

		old_volume = int(player.volume * 100)

		if 0 < new_volume <= 100:
			player.volume = new_volume / 100.0

			return Response('updated volume from %d to %d' % (old_volume, new_volume), reply=True, delete_after=20)

		else:
			if relative:
				raise exceptions.CommandError(
					'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
						old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
			else:
				raise exceptions.CommandError(
					'Unreasonable volume provided: {}%. Provide a value between 1 and 100.'.format(new_volume), expire_in=20)

	async def cmd_queue(self, channel, player):
		"""
		Usage:
			{command_prefix}queue

		Prints the current song queue.
		"""

		lines = []
		unlisted = 0
		andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

		if player.current_entry:
			song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
			song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
			prog_str = '`[%s/%s]`' % (song_progress, song_total)

			if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
				lines.append("Now Playing: **%s** added by **%s** %s\n" % (
					player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
			else:
				lines.append("Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str))

		for i, item in enumerate(player.playlist, 1):
			if item.meta.get('channel', False) and item.meta.get('author', False):
				nextline = '`{}.` **{}** added by **{}**'.format(i, item.title, item.meta['author'].name).strip()
			else:
				nextline = '`{}.` **{}**'.format(i, item.title).strip()

			currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

			if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
				if currentlinesum + len(andmoretext):
					unlisted += 1
					continue

			lines.append(nextline)

		if unlisted:
			lines.append('\n*... and %s more*' % unlisted)

		if not lines:
			lines.append(
				'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix))

		message = '\n'.join(lines)
		return Response(message, delete_after=30)

	async def cmd_clean(self, message, channel, server, author, search_range=50):
		"""
		Usage:
			{command_prefix}clean [range]

		Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
		"""

		try:
			float(search_range)  # lazy check
			search_range = min(int(search_range), 1000)
		except:
			return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True, delete_after=8)

		await self.safe_delete_message(message, quiet=True)

		def is_possible_command_invoke(entry):
			valid_call = any(
				entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
			return valid_call and not entry.content[1:2].isspace()

		delete_invokes = True
		delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

		def check(message):
			if is_possible_command_invoke(message) and delete_invokes:
				return delete_all or message.author == author
			return message.author == self.user

		if self.user.bot:
			if channel.permissions_for(server.me).manage_messages:
				deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
				return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)), delete_after=15)

		deleted = 0
		async for entry in self.logs_from(channel, search_range, before=message):
			if entry == self.server_specific_data[channel.server]['last_np_msg']:
				continue

			if entry.author == self.user:
				await self.safe_delete_message(entry)
				deleted += 1
				await asyncio.sleep(0.21)

			if is_possible_command_invoke(entry) and delete_invokes:
				if delete_all or entry.author == author:
					try:
						await self.delete_message(entry)
						await asyncio.sleep(0.21)
						deleted += 1

					except discord.Forbidden:
						delete_invokes = False
					except discord.HTTPException:
						pass

		return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=15)

	async def cmd_pldump(self, channel, song_url):
		"""
		Usage:
			{command_prefix}pldump url

		Dumps the individual urls of a playlist
		"""

		try:
			info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
		except Exception as e:
			raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

		if not info:
			raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

		if not info.get('entries', None):
			# TODO: Retarded playlist checking
			# set(url, webpageurl).difference(set(url))

			if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
				raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
			else:
				return await self.cmd_pldump(channel, info.get(''))

		linegens = defaultdict(lambda: None, **{
			"youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
			"soundcloud": lambda d: d['url'],
			"bandcamp":   lambda d: d['url']
		})

		exfunc = linegens[info['extractor'].split(':')[0]]

		if not exfunc:
			raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

		with BytesIO() as fcontent:
			for item in info['entries']:
				fcontent.write(exfunc(item).encode('utf8') + b'\n')

			fcontent.seek(0)
			await self.send_file(channel, fcontent, filename='playlist.txt', content="Here's the url dump for <%s>" % song_url)

		return Response(":mailbox_with_mail:", delete_after=20)

	async def cmd_listids(self, server, author, leftover_args, cat='all'):
		"""
		Usage:
			{command_prefix}listids [categories]

		Lists the ids for various things.  Categories are:
		   all, users, roles, channels
		"""

		cats = ['channels', 'roles', 'users']

		if cat not in cats and cat != 'all':
			return Response(
				"Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
				reply=True,
				delete_after=25
			)

		if cat == 'all':
			requested_cats = cats
		else:
			requested_cats = [cat] + [c.strip(',') for c in leftover_args]

		data = ['Your ID: %s' % author.id]

		for cur_cat in requested_cats:
			rawudata = None

			if cur_cat == 'users':
				data.append("\nUser IDs:")
				rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

			elif cur_cat == 'roles':
				data.append("\nRole IDs:")
				rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

			elif cur_cat == 'channels':
				data.append("\nText Channel IDs:")
				tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
				rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

				rawudata.append("\nVoice Channel IDs:")
				vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
				rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

			if rawudata:
				data.extend(rawudata)

		with BytesIO() as sdata:
			sdata.writelines(d.encode('utf8') + b'\n' for d in data)
			sdata.seek(0)

			# TODO: Fix naming (Discord20API-ids.txt)
			await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

		return Response(":mailbox_with_mail:", delete_after=20)


	async def cmd_perms(self, author, channel, server, permissions):
		"""
		Usage:
			{command_prefix}perms

		Sends the user a list of their permissions.
		"""

		lines = ['Command permissions in %s\n' % server.name, '```', '```']

		for perm in permissions.__dict__:
			if perm in ['user_list'] or permissions.__dict__[perm] == set():
				continue

			lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

		await self.send_message(author, '\n'.join(lines))
		return Response(":mailbox_with_mail:", delete_after=20)


	@owner_only
	async def cmd_setname(self, leftover_args, name):
		"""
		Usage:
			{command_prefix}setname name

		Changes the bot's username.
		Note: This operation is limited by discord to twice per hour.
		"""

		name = ' '.join([name, *leftover_args])

		try:
			await self.edit_profile(username=name)
		except Exception as e:
			raise exceptions.CommandError(e, expire_in=20)

		return Response(":ok_hand:", delete_after=20)

	@owner_only
	async def cmd_setnick(self, server, channel, leftover_args, nick):
		"""
		Usage:
			{command_prefix}setnick nick

		Changes the bot's nickname.
		"""

		if not channel.permissions_for(server.me).change_nickname:
			raise exceptions.CommandError("Unable to change nickname: no permission.")

		nick = ' '.join([nick, *leftover_args])

		try:
			await self.change_nickname(server.me, nick)
		except Exception as e:
			raise exceptions.CommandError(e, expire_in=20)

		return Response(":ok_hand:", delete_after=20)

	@owner_only
	async def cmd_setavatar(self, message, url=None):
		"""
		Usage:
			{command_prefix}setavatar [url]

		Changes the bot's avatar.
		Attaching a file and leaving the url parameter blank also works.
		"""

		if message.attachments:
			thing = message.attachments[0]['url']
		else:
			thing = url.strip('<>')

		try:
			with aiohttp.Timeout(10):
				async with self.aiosession.get(thing) as res:
					await self.edit_profile(avatar=await res.read())

		except Exception as e:
			raise exceptions.CommandError("Unable to change avatar: %s" % e, expire_in=20)

		return Response(":ok_hand:", delete_after=20)


	async def cmd_disconnect(self, server):
		await self.disconnect_voice_client(server)
		return Response(":hear_no_evil:", delete_after=20)

	async def cmd_restart(self, channel):
		await self.safe_send_message(channel, ":wave:")
		await self.disconnect_all_voice_clients()
		raise exceptions.RestartSignal

	async def cmd_shutdown(self, channel):
		await self.safe_send_message(channel, ":wave:")
		await self.disconnect_all_voice_clients()
		raise exceptions.TerminateSignal

	async def on_message(self, message):
		await self.wait_until_ready()

		message_content = message.content.strip()
		if not message_content.startswith(self.config.command_prefix):
			return

		if message.author == self.user:
			self.safe_print("Ignoring command from myself (%s)" % message.content)
			return

		if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
			return  # if I want to log this I just move it under the prefix check

		command, *args = message_content.split()  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
		command = command[len(self.config.command_prefix):].lower().strip()

		handler = getattr(self, 'cmd_%s' % command, None)
		if not handler:  # here's where you check if the command isn't in the bot
			conn = pymysql.connect(
				host='localhost',
				port=3306,
				user='root',
				passwd=mysql_password,
				db='discord'
			)
			cur = conn.cursor()
			cur.execute('SELECT commandOwner FROM commands WHERE commandName=\'{}\''.format(command))
			print(cur.description)
			for row in cur:

				if 'node' in row:
					return await self.safe_send_message(message.channel,
														"You entered a valid command but I don't know "
														"how to run it, ask <@372615866652557312> instead.")
				else:
					return print(command + " doesn't exist on any bot.")
			conn.close()
		if message.channel.is_private:
			if not (message.author.id == self.config.owner_id and command == 'joinserver'):
				await self.send_message(message.channel, 'You cannot use this bot in private messages.')
				return

		if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
			self.safe_print("[User blacklisted] {0.id}/{0.name} ({1})".format(message.author, message_content))
			return

		else:
			self.safe_print("[Command] {0.id}/{0.name} ({1})".format(message.author, message_content))

		user_permissions = self.permissions.for_user(message.author)

		argspec = inspect.signature(handler)
		params = argspec.parameters.copy()

		# noinspection PyBroadException
		try:
			if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
				await self._check_ignore_non_voice(message)

			handler_kwargs = {}
			if params.pop('message', None):
				handler_kwargs['message'] = message

			if params.pop('channel', None):
				handler_kwargs['channel'] = message.channel

			if params.pop('author', None):
				handler_kwargs['author'] = message.author

			if params.pop('server', None):
				handler_kwargs['server'] = message.server

			if params.pop('player', None):
				handler_kwargs['player'] = await self.get_player(message.channel)

			if params.pop('permissions', None):
				handler_kwargs['permissions'] = user_permissions

			if params.pop('user_mentions', None):
				handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

			if params.pop('channel_mentions', None):
				handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

			if params.pop('voice_channel', None):
				handler_kwargs['voice_channel'] = message.server.me.voice_channel

			if params.pop('leftover_args', None):
				handler_kwargs['leftover_args'] = args

			args_expected = []
			for key, param in list(params.items()):
				doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
				args_expected.append(doc_key)

				if not args and param.default is not inspect.Parameter.empty:
					params.pop(key)
					continue

				if args:
					arg_value = args.pop(0)
					handler_kwargs[key] = arg_value
					params.pop(key)

			if message.author.id != self.config.owner_id:
				if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
					raise exceptions.PermissionsError(
						"This command is not enabled for your group (%s)." % user_permissions.name,
						expire_in=20)

				elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
					raise exceptions.PermissionsError(
						"This command is disabled for your group (%s)." % user_permissions.name,
						expire_in=20)

			if params:
				docs = getattr(handler, '__doc__', None)
				if not docs:
					docs = 'Usage: {}{} {}'.format(
						self.config.command_prefix,
						command,
						' '.join(args_expected)
					)

				docs = '\n'.join(l.strip() for l in docs.split('\n'))
				await self.safe_send_message(
					message.channel,
					'```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
					expire_in=60
				)
				return

			response = await handler(**handler_kwargs)
			if response and isinstance(response, Response):
				content = response.content
				if response.reply:
					content = '%s, %s' % (message.author.mention, content)

				sentmsg = await self.safe_send_message(
					message.channel, content,
					expire_in=response.delete_after if self.config.delete_messages else 0,
					also_delete=message if self.config.delete_invoking else None
				)

		except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
			print("{0.__class__}: {0.message}".format(e))

			expirein = e.expire_in if self.config.delete_messages else None
			alsodelete = message if self.config.delete_invoking else None

			await self.safe_send_message(
				message.channel,
				'```\n%s\n```' % e.message,
				expire_in=expirein,
				also_delete=alsodelete
			)

		except exceptions.Signal:
			raise

		except Exception:
			traceback.print_exc()
			if self.config.debug_mode:
				await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

	async def on_voice_state_update(self, before, after):

		# logging user movement

		from datetime import datetime

		current_time = datetime.now().strftime("%H:%M %Y-%m-%d ")

		channel_names = []
		server_selection = None
		for channel in after.server.channels:
			channel_names.append(channel.name)

		if "logs" not in channel_names:
			return print("There was a voice activity in {} but there was no logs channel found.".format(after.server.name))
		else:
			for channel in after.server.channels:
				if channel.name == "logs":
					server_selection = channel

		if before.voice_channel is None:
			await self.safe_send_message(server_selection,
										 "```nginx\n{} joined {} at {} (California Time)\n```".format(after.name, after.voice_channel, current_time))
		elif after.voice_channel is None:
			await self.safe_send_message(server_selection, "```nginx\n{} disconnected from {} at {} (California Time)\n```"
										 .format(after.name, before.voice_channel, current_time))
		# voice state is triggered when user mutes but doesn't change channels, don't display that

		elif before.voice_channel == after.voice_channel:
			return
		else:
			await self.safe_send_message(server_selection,
										 "```nginx\n{} switched from {} to {} at {} (California Time)\n```".format(after.name, before.voice_channel, after.voice_channel, current_time))

		print("{} joined {} at {} my time".format(after.name, after.voice_channel, current_time))

		if not all([before, after]):
			return

		if before.voice_channel == after.voice_channel:
			return

		if before.server.id not in self.players:
			return

		my_voice_channel = after.server.me.voice_channel  # This should always work, right?

		if not my_voice_channel:
			return

		if before.voice_channel == my_voice_channel:
			joining = False
		elif after.voice_channel == my_voice_channel:
			joining = True
		else:
			return  # Not my channel

		moving = before == before.server.me

		auto_paused = self.server_specific_data[after.server]['auto_paused']
		player = await self.get_player(my_voice_channel)

		if after == after.server.me and after.voice_channel:
			player.voice_client.channel = after.voice_channel

		if not self.config.auto_pause:
			return

		if sum(1 for m in my_voice_channel.voice_members if m != after.server.me):
			if auto_paused and player.is_paused:
				print("[config:autopause] Unpausing")
				self.server_specific_data[after.server]['auto_paused'] = False
				player.resume()
		else:
			if not auto_paused and player.is_playing:
				print("[config:autopause] Pausing")
				self.server_specific_data[after.server]['auto_paused'] = True
				player.pause()

	async def on_server_update(self, before: discord.Server, after: discord.Server):
		if before.region != after.region:
			self.safe_print("[Servers] \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

			await self.reconnect_voice_client(after)
	#  here's where we would be putting the code that backs up the server after every update

####################################
# My Commands ######################
####################################

	async def cmd_weather(self, channel, author, city_name, leftover_args):
		import json
		import requests
		from datetime import datetime
		contact_message = await self.safe_send_message(channel, "Contacting OpenWeatherMap...")

		city_name_url = "http://api.openweathermap.org/data/2.5/weather?q="
		units = "&units=metric"
		if not city_name:
			return Response('Please enter a city after {}weather'.format(self.config.command_prefix), delete_after=20)
		city = ' '.join([city_name, *leftover_args])
		print((city))
		urlrequest = city_name_url + city + units + weather_api_key
		response = requests.get(urlrequest)
		data = json.loads(response.text)
		print(data)
		if data['cod'] == '404':
			await self.safe_delete_message(contact_message)
			return await self.safe_send_message(channel, "Could not find a city named {}.".format(city))

		cityID = data['id']
		#  getting forecast data
		forecast_url = "http://api.openweathermap.org/data/2.5/forecast?id="

		forecast_request = forecast_url + str(cityID) + units + weather_api_key
		forecast_response = requests.get(forecast_request)
		forecast = json.loads(forecast_response.text)

		date_array = []
		forecast_array = []
		temperature_array = []
		dates = [y['dt'] for y in forecast['list']]
		days = dates[::8]
		for i in range(len(days)):
			date_array.append(datetime.fromtimestamp(float(days[i])).strftime('%m/%d/%Y'))

		for d in forecast['list']:
			for k, v in d.items():
				if v in days:
					print(d)
					forecast_array.append(d['weather'][0]['description'])
					temperature_array.append(d['main']['temp'])

		country = data['sys']['country']

		weather_embed = discord.Embed(
			title='Weather Data for {}:'.format(city.title()),
			type='rich',
			color=author.color,
		)
		main = data['main']
		weather_embed.set_thumbnail(url='http://openweathermap.org/img/w/{}.png'.format(data['weather'][0]['icon']))
		weather_embed.add_field(name='Country', value=country, inline=True)
		weather_embed.add_field(name='Date', value=str(datetime.fromtimestamp(float(data['dt'])).strftime('%m-%d-%Y')), inline=True)
		weather_embed.add_field(name='Temperature', value="{} °C".format(main['temp']), inline=True)
		weather_embed.add_field(name='Weather Description', value=data['weather'][0]['description'].title(), inline=True)
		weather_embed.add_field(name='Humidity', value="{} %".format(main['humidity']), inline=True)
		weather_embed.add_field(name='Pressure', value="{} kPa".format(main['pressure']/10), inline=True)
		for i in range(len(forecast_array)):
			weather_embed.add_field(name="Forecast for {}".format(date_array[i]), value="{}\n{} °C"
									.format(forecast_array[i].title(), temperature_array[i]), inline=True)

		await discord.Client.send_message(self, destination=channel, embed=weather_embed)
		await self.safe_delete_message(contact_message)

	async def cmd_coin(self, channel,author):
		import random

		flip = ["Heads", "Tails"]

		rand_number = random.randrange(0, 2)

		cards = [':dollar:', ':yen:', ':euro:', ':pound:']
		hand = await self.send_message(channel, ' '.join(cards))
		await asyncio.sleep(0.6)

		for x in range(4):
			shuffle(cards)
			await self.safe_edit_message(hand, ' '.join(cards))
			await asyncio.sleep(0.6)
		await self.safe_delete_message(hand, quiet=True)

		return Response("{} flipped: {}".format(author.name, flip[rand_number]), delete_after=30)

################ TRANSLATE ####################################################################
	async def cmd_translate(self, author, channel, user_input, leftover_args):
		"""
		Usage:
			{cmd_prefix}translate [user_input]

		"""
		if not user_input:
			raise exceptions.CommandError(
				"Usage: \n !translate [message] \n\nTranslates message to English.")

		user_input = ' '.join([user_input, *leftover_args])

		from googletrans import Translator
		translator = Translator()


		# print(type(text))

		detection = translator.detect(user_input)
		text = translator.translate(user_input)
		return Response(str(detection) + "\n" + str(text))


	async def cmd_jointime(self, server, author):
		"""
		!jointime

		Returns the date user joined the server
		"""
		time = author.joined_at.strftime("%m-%d-%Y")

		return Response("{} joined {} at {}.".format((author.name),(server), (time)))

	async def cmd_testme(self,channel):
		await self.safe_send_message(channel, "MY NIGGAS", expire_in=20)

	async def cmd_report(self, channel):
		await self.safe_send_message(channel, "Fuck you Weien.", expire_in=20)

	async def cmd_weien(self, channel, author):
		import random
		from musicbot import weien
		number = random.randrange(0, len(weien.weien_questions))

		question_selection = weien.weien_questions[number]
		answer_selection = weien.weien_answers[number]
		correct_selection = weien.weien_correct[number]

		prompt = await self.safe_send_message(channel,"Fact # [" + str(number+1) +"/" +str(len(weien.weien_questions)) +"]\nFill in the blanks:\n\n{}\n\n".format(question_selection),expire_in=60)
		print(str(question_selection) + "\n" + str(answer_selection))

		iter = 0
		not_right_exists = False
		while True:
			answer = await self.wait_for_message(20, author=author, channel=channel)
			if not answer:
				if not_right_exists == True:
					await self.safe_delete_message(not_right)
				return
			elif answer.content.startswith(self.config.command_prefix):
				await self.safe_delete_message(prompt)
				return
			elif answer.content == answer_selection:
				if not_right_exists == True:
					await self.safe_delete_message(not_right)
				return Response("You answered: "+ answer.content +"\n\nThat's correct! " + correct_selection)
			else:
				if not_right_exists == True:
					await self.safe_delete_message(not_right)
				hint = answer_selection[:iter + 1]
				if iter == 3:
					return Response("Wow you're a dumbass, the answer is: " + answer_selection + ".\n"
									+ correct_selection)
				if question_selection == weien.weien_questions[4] and answer.content == ('galician' or 'Galician'):
					await self.safe_send_message(channel,
					"You answered: "+answer.content+ "\n\nCommon misconception. The Swiss population does not actually speak Galician or require"
					" their citizens to learn Galician. https://en.wikipedia.org/wiki/Languages_of_Switzerland"
					".\nHere's a hint: " + str(hint) + "\nHints left: " + str(2 - iter), expire_in=20)
					continue
				not_right = await self.safe_send_message(channel,"You answered: "+answer.content + "\n\nNot quite right, here's a hint: " + str(hint)
												 + "\nHints left: " + str(2-iter), expire_in=20)
				not_right_exists = True
				iter += 1
			await self.safe_delete_message(answer)


	async def cmd_website(self, channel):
		return await self.safe_send_message(channel, "Login: http://68.4.235.189:8080/\nGame: http://68.4.235.189:8080/Game/landing.php#")

	async def cmd_mari(self):
		return Response("Pika Pika!")

	async def cmd_mosti(self, channel, author):
		await self.safe_send_message(channel, "Mosti thought generator\nPlease enter the number of lines "
											  "of Mosti thoughts you'd like to generate.")
		response = await self.wait_for_message(20, author=author, channel=channel)
		if not response:
			return
		if response.content.isdigit() == False:
			return Response("That's not a number you idiot.")
		elif int(response.content) > 100:
			raise exceptions.HelpfulError("Mostafa is not capable of having that many thoughts.",
										  "Enter less things to think about.")
		array = []
		for i in range(0, int(response.content)):
			array.append("{}: {}".format(str((i+1)),("Easy Kids").rjust(9," ")))
		response = '\n'.join(array)
		return Response(response)

################ COOKIES ##############################################################################################

	async def cmd_cookies(self, channel):
		import sqlite3
		from texttable import Texttable
		conn = sqlite3.connect('D:\\Program Files (x86)\\SQLite\\SQLiteStudio\\Discord.db')
		c = conn.cursor()

		c.execute("SELECT username, cookie_value FROM cookies ORDER BY cookie_value DESC")
		result = c.fetchall()

		table = Texttable()
		table.set_cols_align(["l", "r"])
		table.set_cols_valign(["m", "m"])
		table.add_rows([["User", "Cookies"], *result])

		Response("```{}```".format(table.draw()))
		return await self.safe_send_message(channel, "If you actually use this command and it's too long and annoying "
													 ", complain to <@{}>". format(self.config.owner_id))



	async def cmd_congratz(self, channel, author, user_mentions):
		import sqlite3
		"""
		Usage:
			{cmd_prefix}congratz [@user]

		Congratulates the fuck out of a user.
		"""

		if not user_mentions:
			raise exceptions.CommandError(
				"Usage: \n !congratz [@user] \n\nCongratulates the fuck out of a user.")

		usr = user_mentions[0]
		cookie_amount = 1

		if usr.id == author.id:
			return Response("Are you seriously congratulating yourself? That's pathetic, no cookies for you.", delete_after=20)

		await self.safe_send_message(channel, "Wow holy shit " + usr.name +" congratulations you must feel SO good about yourself!\n\n"
											   + str(cookie_amount) +" :cookie: awarded.")

		## all catching of faulty input/illegal commands caught
		# starting SQL process
		conn = sqlite3.connect('D:\\Program Files (x86)\\SQLite\\SQLiteStudio\\Discord.db')
		c = conn.cursor()
		# checking for table

		c.execute('CREATE TABLE IF NOT EXISTS cookies(userid INT, username TEXT, cookie_value INT)')
		print("created table")
		conn.commit()


		exists = c.execute('SELECT COUNT(*)FROM cookies WHERE userid == {}'.format(usr.id))
		exist_return = exists.fetchone()
		print(exist_return)
		if exist_return[0] == 1:
			print("User found")
		elif exist_return[0] == 0:
			# TODO: see if it's possible assign this a variable and use it to print out 1 to avoid looking through the db for no reason
			c.execute('INSERT INTO cookies(userid,username,cookie_value) VALUES({}, {}, {})'.format(usr.id, "\"" + str(usr.name) + "\"", 0))
		else:
			error = c.execute('SELECT * FROM cookies WHERE userid = {}'. format(usr.id))
			error = error.fetchall()[0]
			return Response("ERROR:\n\tDEBUGGING: Multiple entries of userid were found, printing all rows\n" + str(error))

		c.execute('UPDATE cookies SET cookie_value = cookie_value + {} WHERE userid = {}'.format(cookie_amount, usr.id))
		# in case the person changed their name since the last time they used the command
		# klkc.execute('UPDATE cookies SET username = {} WHERE userid = {}'.format(usr.name, usr.id))
		current_cookies = c.execute('SELECT cookie_value from cookies WHERE userid = {}'.format(usr.id))
		current_cookies = current_cookies.fetchone()[0]

		await self.safe_send_message(channel, "{} has {} cookies in total".format(usr.name,current_cookies))
		conn.commit()
		c.close()
		conn.close()

###################### SERVER #######################################################################################
	async def cmd_server(self, channel, author):
		import subprocess
		import os
		server_running = True

		s = os.popen("tasklist").read()
		if "javaw.exe" or "java.exe" in s:
			await self.send_typing(channel)
			clearlist = await self.safe_send_message(channel, "Minecraft server seems to be running.", expire_in=20)
			clearlist2 = await self.safe_send_message(channel, "Would you like to restart it? (y, n)", expire_in=20)
		else:
			server_running = False
			await self.send_typing(channel)
			clearlist = await self.safe_send_message(channel, "Minecraft server is not running.", expire_in=20)
			clearlist2 = await self.safe_send_message(channel, "Would you like to start the server? (y, n)", expire_in=20)
		start_prompt = await self.wait_for_message(20, author=author, channel=channel)

		try:
			if server_running == False:
				await self.safe_delete_message(clearlist)
				await self.safe_delete_message(clearlist2)
				if start_prompt.content.startswith("y"):
					p = subprocess.Popen("newrun.bat", cwd=r"C:\\Users\\Ali\\Bukkit",
										 shell=True)
					await self.send_typing(channel)
					s = os.popen("tasklist").read()
					if "javaw.exe" or "java.exe" in s:
						await self.send_typing(channel)
						await self.safe_send_message(channel, "Server is now running.", expire_in=20)
					else:
						await self.send_typing(channel)
						await self.safe_send_message(channel, "Server could not start up.", expire_in=20)
				elif start_prompt.content.startswith("n"):
					await self.safe_send_message(channel, "Alright.", expire_in=20)
				elif not start_prompt:
					await self.send_safe_message(channel, "No response gotten.")
				else:
					await self.safe_send_message(channel, "Not an appropriate response, quitting.", expire_in=20)
			else:
				await self.safe_delete_message(clearlist)
				await self.safe_delete_message(clearlist2)
				if start_prompt.content.startswith("y"):
					await self.safe_send_message(channel, "Restarting server...", expire_in=5)
					os.system('TASKKILL /F /IM java.exe')
					os.system('TASKKILL /F /IM javaw.exe')
					p = subprocess.Popen("newrun.bat", cwd=r"C:\\Users\\Ali\\Bukkit",
										 shell=True)
					stdout, stderr = p.communicate()
					await self.send_typing(channel)
					s = os.popen("tasklist").read()
					if "javaw.exe" or "java.exe" in s:
						await self.send_typing(channel)
						await self.safe_send_message(channel, "Server was successfully restarted.", expire_in=20)
					else:
						await self.send_typing(channel)
						await self.safe_send_message(channel, "Server couldn't restart.", expire_in=20)
		except AttributeError as e:
			# shitty error handling to prevent discord from giving errors when things time out
			pass
		try:
			await self.safe_delete_message(start_prompt)
		except:
			pass

	async def cmd_map(self, channel, author):
		import os
		path = "C:\\Users\\Ali\\Bukkit"
		address = "C:\\Users\\Ali\\Bukkit\\server.properties"
		fread = open(address, 'r')
		lines = fread.readlines()

		fread = open(address)
		for i, line in enumerate(fread):
			if i == 17:
				current_map_message = await self.safe_send_message(channel, "Current Map: " + line.split("=")[1]+ "\n\n", expire_in=20)
		fread.close()

		# adds all folder names to list
		location = next(os.walk(path))[1]

		for i in location:
			if '_nether' in i:
				location.remove(i)
		# have to use two different for loops to remove keywordd-containing items from location for some reason
		for i in location:
			if '_the_end' in i:
				location.remove(i)

		location.remove("logs")
		location.remove("src")
		location.remove("target")
		location.remove("plugins")
		location.remove("crash-reports")
		location.remove('.git')


		format = ('\n'.join('{}: {}'.format(*k) for k in enumerate(location)))

		existing_worlds = await self.safe_send_message(channel, "Existing worlds:" + "\n\n" + format + "\n", expire_in=20)
		select_world = await self.safe_send_message(channel, "Select a number corresponding to a world." + "\n\n", expire_in=20)
		selection = await self.wait_for_message(20, author=author, channel=channel)

		if not selection:
			return Response("Nevermind.", delete_after=20)
		try:
			if selection.content:
				if selection.content.isdigit():
					global world
					world = location[int(selection.content)]
				else:
					# another !command is given as response
					if selection.content.startswith(self.config.command_prefix):
						await self.safe_delete_message(existing_worlds)
						await self.safe_delete_message(select_world)
						await self.safe_delete_message(current_map_message)
						return
					else:
						world = str(selection.content)
			if selection.content.isnumeric() == False:
				raise exceptions.HelpfulError("You did not enter a number, exiting", "Please enter a number that corresponds with the map.")
				pass
		except Exception as e:
			pass

		with open(address, 'w') as f:
			for i, line in enumerate(lines):
				if i == 17:
					print(line)
					f.write('level-name=' + world + "\n")
					print(line)
					continue
				f.write(line)
		if selection:
			await self.safe_send_message(channel, "Map successfully changed to: " + world +
										 "\n\n" + "Restart server using !server.", expire_in= 60)
			await self.safe_delete_message(existing_worlds)
			await self.safe_delete_message(select_world)
			await self.safe_delete_message(selection)
			print("Map changed to" + world + " by " + str(author))


	# global variable that changes for !again command
	perm_link_list = {}
############################### IMGUR ####################################
	async def imgur_search_mode(self, array, iterator, channel, existingauthor):
		from urllib.request import urlopen
		import os

		# pretty stupid to get existingauthor as a parameter from the previous method since author is the same..
		iterated = array[iterator]

		await self.safe_send_message(channel, "Picture # : [" + str(iterator+1) + '/' + str(len(array)) + '] @ ' +
									 self.perm_link_list[str(existingauthor)]['title'])

# TODO: Make a single method that takes the extension as an argument, move picture identifier inside method to
# TODO: minimize the time difference between sending the text and the picture so ppl don't type between the msgs

		if str(iterated).endswith('.jpg'):
			with urlopen(iterated) as URL:
				with open('temp.jpg', 'wb') as f:
					f.write(URL.read())
					await discord.Client.send_file(self, channel, self.dirname + '\\temp.jpg')
					f.close()
					os.remove('temp.jpg')
		elif str(iterated).endswith('.gif'):
			with urlopen(iterated) as URL:
				with open('temp.gif', 'wb') as f:
					f.write(URL.read())
					await discord.Client.send_file(self, channel, self.dirname + '\\temp.gif')
					f.close()
					os.remove('temp.gif')
		elif str(iterated).endswith('.png'):
			with urlopen(iterated) as URL:
				with open('temp.png', 'wb') as f:
					f.write(URL.read())
					await discord.Client.send_file(self, channel, self.dirname + '\\temp.png')
					f.close()
					os.remove('temp.png')


	async def cmd_imgur(self, author, channel, server):
		import requests
		import json
		import random

		print(channel.id)
		print ('311565508652564490')
		header = {'authorization': 'Client-ID ' + CLIENT_ID}
		auth_header = {'authorization': 'Bearer ' + ACCESS_TOKEN}

		# don't let people post in lobby
		if channel.id == '311565508652564490':
			await self.safe_send_message(channel, "This command is not allowed in Lobby.", expire_in=15)
			return

		# Searches for the album
		r = requests.get('https://api.imgur.com/3/account/DiscordPictureWizard/albums/', headers=auth_header)
		try:
			albumparse = json.loads(r.text)
		except json.decoder.JSONDecodeError as e :
			await self.safe_send_message(channel, "Error accessing Imgur API.")
			print(e)
			return

		# successfully queried the API?
		data = albumparse["data"]
		if albumparse['success'] == True:
			pass
		else:
			await self.safe_send_message(channel, "ERROR: There was a problem accessing the album information "
					"on Imgur \n\nCopying JSON response:")
			print(albumparse)
			await self.safe_send_message(channel, str(albumparse['data']['error']))
			return

		albums_title = []
		albums_identifier = []
		albums_id = []



		# checking if there are any albums in imgur, there will always be one so it's kinda useless
		if albumparse['data'] == False:
			await self.safe_send_message(channel, "No albums found.")
			return

		# deleting the previous entry for the author and creating a new one. Maybe a replace function exists?
		if str(author) in self.perm_link_list:
			del self.perm_link_list[str(author)]
		self.perm_link_list[str(author)] = {}
		all_lines = []

		album_header_message = await self.safe_send_message(channel, "\n" + "Albums found for " + str(author.name) + ":")

		# printing the title and the number corresponding to the albums
		for identifier, title in enumerate(d['title'] for d in data):
			albums_title.append(title)
			albums_identifier.append(identifier+1)
			all_lines.append(str(identifier+1) + ": " + title)

		# prints all found items one under each other to avoid spamming the chat and getting flood protection'd
		formatted = ("\n".join(map(str, all_lines)))

		album_message = await discord.Client.send_message(self, channel, formatted)


		for i in albumparse['data']:
			for k, v in i.items():
				if k == 'id':
					albums_id.append(v)

		album_selection = await self.wait_for_message(20, author=author, channel=channel)

		# user doesn't type anyting
		if not album_selection or type(album_selection) == None:
			await self.safe_send_message(channel, "Oh well.", expire_in=20)
			await self.safe_delete_message(album_message)
			await self.safe_delete_message(album_header_message)
			return

		# user started a new search because they have autism or something so clean up and exit function
		if album_selection.content.startswith(self.config.command_prefix):
			await self.safe_delete_message(album_message)
			await self.safe_delete_message(album_header_message)
			return

		# is album nsfw? don't allow NSFW in non-NSFW channels
		if channel.name != "nsfw":
			# go through ALL dictionaries in 'data': [list]
			for i in albumparse['data']:
				# is the title of the album selected in the specific dictionary we're iterating?
				for k,v in i.items():
					if v == albums_title[int(album_selection.content)-1]:
						# if so, look over all the keys to find description, iterate over entire i again to check:
						for k, v in i.items():
							if k == 'description' and v == 'nsfw':
								await self.safe_delete_message(album_message)
								await self.safe_delete_message(album_header_message)
								return Response("NSFW albums can only be posted on NSFW channels.", delete_after=20)


		# setting the album name in {author: {'title': title ... }
		# since the order of the loop doesn't change, we don't have to match up selections with our data.
		# our data[our_selection -1(since the array we're displaying on discord starts at 1, not 0)]
		# will always give us the right album
		self.perm_link_list[str(author)]['title'] = albums_title[int(album_selection.content) - 1]


		image_information = requests.get('https://api.imgur.com/3/album/'+ albums_id[int(album_selection.content)-1] +'/images', headers=header)
		image_info_json = json.loads(image_information.text)
		image_data = image_info_json['data']

		link_list = []
		response_titles = []

		# finding link name of pictures in reply
		for i in image_data:
			for k, v in i.items():
				if k == 'link':
					if 'links' not in self.perm_link_list:
						# leaving this none otherwise appending a list onto an empty list creates double list
						self.perm_link_list[str(author)]['links'] = None
					link_list.append(v)

		self.perm_link_list[str(author)]['links'] = link_list

		# finding titles of pictures and links in reply
		for i in image_data:
			for k, v in i.items():
				if k == 'title':
					response_titles.append(v)

		# selecting random picture from album using randrange, randint isn't inclusive
		image_choice = random.randrange(0, len(link_list), 1)

		await self.safe_delete_message(album_selection)
		await self.safe_delete_message(album_message)
		if album_header_message:
			await self.safe_delete_message(album_header_message)
		await self.imgur_search_mode(link_list, image_choice, channel, author)


	async def cmd_again(self, channel, author):
		import random
		# no images in lobby
		if channel.name == 'lobby':
			await self.safe_send_message(channel, "This command is not allowed in Lobby.")
			return

		# no one has used !imgur before?
		if not self.perm_link_list:
			return Response("Must use !imgur command before posting another random pic from the same album", delete_after=20)

		# has user previously used !again?
		if str(author) in self.perm_link_list:
			try:
				# browsing keys in dict
				for k, v in self.perm_link_list[str(author)].items():
					if k == "links":
						new_link_list = v
						image_choice = random.randrange(0, len(new_link_list),1)
						print("iterator:" +  str(image_choice))
						await self.imgur_search_mode(new_link_list, image_choice, channel, author)
			# somehow list gets created without links being added
			except KeyError:
				await self.safe_send_message(channel, "ERROR: Debug: !imgur did not put in 'links' key in perm_link_list"
					 "while creating list.")
				return
		else:
			return Response("!again sends a random picture from an imgur album previously selected by"
							" the specific user of the command. Someone has used '!imgur' before, but it"
							" wasn't you.", delete_after=20)
	async def cmd_download(self):
		"""
		Usage:
			!download
		Sends the most up-to-date link to download the Imgur Uploader.
		"""
		return Response("Here is the download link for the ImgurUploader https://www.dropbox.com/s/sihwa1x0ss1xsyu/ClientUploader.exe?dl=1&m=")
################################ CYANIDE AND HAPPINESS ########################
	async def cmd_ch(self,channel):
		"""
		Usage:
			!ch
		Sends a random cyanide and happiness comic.
		"""
		from bs4 import BeautifulSoup
		import requests
		import random
		from urllib.request import urlopen

		while True:
			number = random.randrange(0, 4811)
			r = requests.get("http://explosm.net/comics/{}/".format(number))
			if r.text == "Could not find comic":
				continue
			else:
				break

		soup = BeautifulSoup(r.text, "html.parser")
		imgs = soup.find("img", {"id": "main-comic"})

		link = imgs['src']
		ximg = link.split("//")[1]
		ximg = ("http://{}".format(ximg))

		await self.safe_send_message(channel, "Comic #: [{}/{}]".format(number, 4751))
		with urlopen(ximg) as URL:
			with open('cyanide.jpg', 'wb') as f:
				f.write(URL.read())
				await discord.Client.send_file(self, channel, self.dirname + '\\cyanide.jpg')
				f.close()
				os.remove('cyanide.jpg')

############################## GIPHY API ##############################################################

	async def cmd_gif(self, author, channel, leftover_args, parameter=None):
		'''
		Usage:
		{command_prefix}gif [parameter]

		Searches gifs from giphy and sends the first result.
		Sends random trending if no parameters are given.
		'''
		import requests
		from urllib.request import urlopen
		import json

		wait_message = await self.safe_send_message(channel, "Attempting to download the highest quality GIF from GIPHY...\n"
															 "This might take a bit depending on how big the gif is.")
		# such fucking spaghetti code

		async def sendgif(parameter, withparameter, withoutparameter):
			if parameter:
				parameter = ' '.join([parameter, *leftover_args])
				r = requests.get("https://api.giphy.com/v1/gifs/search?api_key={}&q={}&limit=1&offset=0&rating=G&lang"
								 "=en".format(GIPHY_API_KEY, parameter))
				rjson = json.loads(r.text)

				self.giphyurl = rjson['data'][0]['images'][withparameter]['url']
			else:
				no_param = await self.safe_send_message(channel, "No search parameter provided, sending random gif.")
				r = requests.get("https://api.giphy.com/v1/gifs/random?api_key={}&tag=&rating=R".format(GIPHY_API_KEY))
				rjson = json.loads(r.text)

				for k, v in rjson['data'].items():
					if k == withoutparameter:
						self.giphyurl = v

			with urlopen(self.giphyurl) as URL:
				with open('giphy.gif', 'wb') as f:
					f.write(URL.read())
					await discord.Client.send_file(self, channel, self.dirname + "\\giphy.gif")
					f.close()
					os.remove('giphy.gif')

			try:
				await self.safe_delete_message(no_param)
			except UnboundLocalError: # bad practice passing exceptions but in this case it doesn't really matter
				pass
			await self.safe_delete_message(wait_message)
		try:
			await sendgif(parameter, 'original', 'image_original_url')

		except discord.errors.HTTPException:  # file too big
			compress = await self.safe_send_message(channel, "I found a gif but it was too big to send, trying to "
															 "compress it.")
			await self.safe_delete_message(wait_message)
			try:
				await sendgif(parameter, 'downsized_medium', 'fixed_width_small_url')
				await self.safe_delete_message(compress)
			except discord.errors.HTTPException: # file STILL too big
				await self.safe_delete_message(compress)
				await self.safe_send_message(channel, "Either something is wrong with Discord or even the compressed"
													  " version of this gif is still too large to send, bummer.")
				return
	async def cmd_shittybot(self, author, channel):
		return await self.safe_send_message(channel, "Maybe a little bit but definitely not as much as Mee6")

	async def cmd_eval(self, author, channel, leftover_args, parameter=None):
		if parameter:
			args = " ".join([parameter, *leftover_args])
			print("leftover_args: " + args)
			evalled = await self.safe_send_message(channel, eval(args))
			if not evalled:
				return
			return evalled


	async def cmd_stab(self, author, channel, user_mentions):
		import random
		if not user_mentions:
			return await self.safe_send_message(channel, "Mention a user, I'm not gonna stab myself.")
		usr = user_mentions[0]
		count = random.randrange(1, 100)
		return await self.safe_send_message(channel, "{} just stabbed {} {} times".format(author.name, usr.name, count))

	async def ordered_channels(self, inputserver):
		text_channels = []
		voice_channels = []
		for iterchannel in inputserver.channels:
			print("{}, type: {}, position: {} \n".format(iterchannel.name, iterchannel.type, iterchannel.position))
			if iterchannel.type == 4:
				continue
			elif str(iterchannel.type) == 'voice':
				voice_channels.append(iterchannel)
			elif str(iterchannel.type) == 'text':
				text_channels.append(iterchannel)
		text_channels.sort(key=lambda x: x.position)
		voice_channels.sort(key=lambda x: x.position)
		array = [text_channels, voice_channels]
		return array


	async def cmd_backup(self, server, channel, author):
		"""
		Usage: {cmd_prefix}backup

		Backs up the server's users and channels to later reroll to.
		To preserve the order of the channels when rerolling, keep voice channels under text channels.
		"""
		import datetime
		import emoji
		import re
		returnarray = await self.ordered_channels(server)
		text_channels = returnarray[0]
		voice_channels = returnarray[1]
		sendstr = ""
		for i in text_channels:
			sendstr += "{}, type: {}, position: {} \n".format(i, i.type, i.position)
		for i in voice_channels:
			sendstr += "{}, type: {}, position: {} \n".format(i, i.type, i.position)
		if sendstr == "":
			return await self.safe_send_message(channel, "Channel list is empty... for some reason.")
		print(sendstr)
		server_name = str(server.name.lower()) #  sql only accepts servers with lower case names
		server_name = re.escape(emoji.demojize(server_name)) #  escaping dumbass emojis and punctuation
		now = datetime.datetime.now()
		date = now.strftime("%Y-%m-%d")
		#date = "2017-11-18"
		conn = pymysql.connect(
			host='localhost',
			port=3306,
			user='root',
			passwd=mysql_password,
			db='discord_channel_backup'
		)
		cur = conn.cursor()
		check_table = "SELECT count(*) FROM information_schema.TABLES WHERE table_name = '{}'".format(re.escape(server_name))
		cur.execute(check_table)
		result = cur.fetchone()
		if '0' in str(result):
			create_table = "CREATE TABLE `{}`(`channel_name` TEXT, `channel_type` TEXT , `position` INT, `date` VARCHAR(255))".format(server_name)
			cur.execute(create_table)
			await self.safe_send_message(channel, "{} was not found on the database but a save file for it was successfully created.".format(server.name))
		elif '1' in str(result):
			check_date = "SELECT * FROM `{}` WHERE date = '{}'".format(server_name, date)
			cur.execute(check_date)
			date_result = cur.fetchall()
			if date_result:
				#  checking class
				return await self.safe_send_message(channel, "All channels were already backed up today.")
		for i in text_channels:
			sql = "INSERT INTO `{}`(`channel_name`, `channel_type`, `position`, `date`) VALUES('{}','{}','{}','{}')".format(server_name, i.name, i.type, i.position, date)
			cur.execute(sql)
		for i in voice_channels:
			sql = "INSERT INTO `{}` (`channel_name`, `channel_type`, `position`,`date`) VALUES ('{}','{}','{}','{}')".format(
				server_name, i.name, i.type, i.position, date)
			cur.execute(sql)
		conn.commit()
		conn.close()
		# checking users
		"""
		conn = pymysql.connect(
			host='localhost',
			port=3306,
			user='root',
			passwd=mysql_password,
			db='discord_user_backup'
		)
		cur = conn.cur()
		users = server.members
		check_table = "SELECT count(*) FROM information_schema.TABLES WHERE table_name = '{}'".format(
			re.escape(server_name))
		cur.execute(check_table)
		result = cur.fetchone()
		if '0' in str(result):
			create_table = "CREATE TABLE `{}`(`username` TEXT, `discordID` TEXT , `date` VARCHAR(255))".format(
				server_name)
			cur.execute(create_table)
			await self.safe_send_message(channel,
										 "{} was not found on the database but a table for it was successfully created.".format(
											 server.name))
		elif '1' in str(result):
			check_date = "SELECT * FROM `{}` WHERE date = '{}'".format(server_name, date)
			cur.execute(check_date)
			date_result = cur.fetchall()
			if date_result:
				#  checking class
				return await self.safe_send_message(channel, "All users were already backed up today.")
		for i in users:
			sql = "INSERT INTO `{}`('','')"
		"""
		await self.safe_send_message(channel, "Backed server up successfully! :thumbsup:")

	async def cmd_date(self, channel):
		import datetime
		now = datetime.datetime.now()
		newdate = now.strftime("%Y/%m/%d")
		return await self.safe_send_message(channel, "The date is: " + str(newdate))

	async def cmd_check(self, server, channel, author):
		import re
		import emoji
		from texttable import Texttable
		from terminaltables import AsciiTable
		conn = pymysql.connect(
			host='localhost',
			port=3306,
			user='root',
			passwd=mysql_password,
			db='discord_channel_backup'
		)
		server_name = str(server.name.lower())  # sql only accepts servers with lower case names
		server_name = emoji.demojize(server_name)  # escaping dumbass emojis and punctuation

		cur = conn.cursor()
		search_query = "SELECT DISTINCT date FROM `{}` ".format(re.escape(server_name))
		cur.execute(search_query)

		dates = cur.fetchall()
		print(dates)
		all_backups = {}
		for i in dates:
			print(i[0])
			channel_count = "SELECT count(*) FROM `{}` WHERE date = '{}'".format(re.escape(server_name), i[0])
			cur.execute(channel_count)
			channel_count = cur.fetchall()
			for count in channel_count:
				all_backups[i[0]] = [count[0]]
		table = Texttable()
		table.set_cols_align(["l", "r"])
		table.set_cols_valign(["m", "m"])
		dumptable = []
		for i, (k, v) in enumerate(all_backups.items()):
			sub = []
			sub.append(i+1)
			sub.append(k)
			sub.append(*v)
			dumptable.append(sub)
		print(*dumptable)

		rows = [["Number", "Backup Date", "# of Channels"], *dumptable]
		table = AsciiTable(rows)
		print(table.table)
		await self.safe_send_message(channel, "```{}```".format(table.table))
		await self.safe_send_message(channel, "[Backup]: Dates are shown in ISO 8601 format to avoid confusion YYYY/MM/DD\n[Backup]: Write the number of the backup you wish to select")

		response_message = await self.wait_for_message(30, author=author, channel=channel)
		if not response_message.content.isdigit():
			return await self.safe_send_message(channel, "[Backup]: You did not enter a valid number.")

		channels_to_restore = {}
		selected_date = ""
		for i in dumptable:
			print(response_message.content)
			print(i[0])
			if str(response_message.content) == str(i[0]): #  selected date found
				print(i[1])
				selected_date = i[1]
		if selected_date == "":
			return await self.safe_send_message(channel, "There was a problem fetching dates from MySQL.")
		cur = conn.cursor()
		channel_name_query = "SELECT channel_name, channel_type, position FROM `{}` WHERE date = '{}' ORDER BY position ASC".format(re.escape(server_name), selected_date)
		cur.execute(channel_name_query)
		channel_names = cur.fetchall()
		text_channels = []
		voice_channels = []
		for x in channel_names:
			print(x[2])
			if str(x[2]) == 'text':
				text_channels.append(x)
			elif str(x[2]) == 'voice':
				voice_channels.append(x)
		print(channel_names)
		print(text_channels)
		for elem in range(len(text_channels)):
			print("element")
			print(channels_to_restore)
			channels_to_restore[text_channels[elem][0]] = text_channels[elem][1]
		for elem in range(len(voice_channels)):
			channels_to_restore[voice_channels[elem][0]] = voice_channels[elem][1]
		returnarray = await self.ordered_channels(server)
		current_channels = []
		text_channels = returnarray[0]
		voice_channels = returnarray[1]
		for i in text_channels:
			current_channels.append(i.name)
		for i in voice_channels:
			current_channels.append(i.name)
		print(current_channels)
		print(channels_to_restore)
		final = {k: v for k, v in channels_to_restore.items() if k not in current_channels}
		print(final)
		if not final:
			await self.safe_send_message(channel, "No missing channels found.")
		#  deleting matching
		#  for i in server.channels:
		print(channels_to_restore)
		#  await self.safe_send_message(channel, final)

		#  for k,v in channels_to_restore.items():
		#    self.create_channel(server, k, v)

		# return await self.safe_send_message(channel,)

	async def cmd_deletdis(self, channel):
		await self.safe_send_message(channel, "You were banned for using the word 'Nigger'")

	async def cmd_universe(self, channel, leftover_args):
		import os
		scriptDir = os.path.dirname(__file__)

		args = ' '.join(leftover_args)
		print(args)
		newargs = args.split(',')
		print(newargs)
		if len(newargs) != 4:
			return await self.safe_send_message\
				(channel,
				 "This command currently only supports 4"
				 " frame memes, yours has {} arguments (separate them with commas)".format(len(newargs)))
		brain = memes.Brain()
		brain.generate_meme(newargs)
		brain.settext4(newargs)
		await discord.Client.send_file(self, channel, scriptDir + '\\meme_folder\\test.jpg')

	async def cmd_poll_users(self, channel):

		talking_users = [i.name for i in channel.server.members if i.voice.voice_channel is not None and not i.bot]
		online_users = [i.name for i in channel.server.members if i.status == discord.Status.online and not i.bot]
		idle_users = [i.name for i in channel.server.members if i.status == discord.Status.idle and not i.bot]

		talk = '{} users currently online. (excluding bots)'.format(len(online_users))
		online = '{} users currently talking. (excluding bots) '.format(len(talking_users))
		idle = '{} users currently idle. (excluding bots) '.format(len(idle_users))

		await self.safe_send_message(channel, talk + "\n" + idle +'\n'+  online)

	async def whatsapp(self):
		#import threading
		#await self.wait_until_ready()
		#self.thd = ThreadedServer('68.4.235.189', 8080, self)
		#threading.Thread(target=self.thd.listen).start()
		pass

	async def fetch_whatsapp_info(self):
		# getting the whatsapp discord server
		server = self.get_server('356166885294997505')
		talking_users = [i.name for i in server.members if i.voice.voice_channel is not None and not i.bot]
		online_users = [i.name for i in server.members if i.status == discord.Status.online and not i.bot]
		idle_users = [i.name for i in server.members if i.status == discord.Status.idle and not i.bot]

		talk = '{} users currently online.'.format(len(online_users))
		online = '{} users currently talking.'.format(len(talking_users))
		idle = '{} users currently idle.'.format(len(idle_users))
		total = '{} Information:\n\n{}\n{}\n{}'.format(server.name, online, idle, talk)

		await self.thd.sendDiscordInformation(total)


if __name__ == '__main__':
	bot = MusicBot()
	bot.run()
