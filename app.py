import asyncio
import json
import logging
import os
import sys
import html as _html
from typing import List, Optional, Dict, Any

from telethon import TelegramClient, errors, events


class MirrorBot:

	def __init__(
		self,
		api_id: int,
		api_hash: str,
		session_name: str,
		raw_mappings: Optional[Dict[Any, List[Any]]] = None,
		delay: float = 2.0,
		enable_logs: bool = True,
		keywords: Optional[List[str]] = None,
	):
		self.api_id = api_id
		self.api_hash = api_hash
		self.session_name = session_name
		# raw_mappings: { source: [dest, ...], ... } accept ints or strings
		self.raw_mappings = raw_mappings or {}
		# resolved mapping will be filled after client start: { int_source_id: [int_dest_id, ...] }
		self.mappings: Dict[int, List[int]] = {}
		self.delay = float(delay)
		self.enable_logs = enable_logs
		self.keywords = [k.lower() for k in keywords] if keywords else []

		if enable_logs:
			logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
		else:
			logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] %(levelname)s: %(message)s")

		self.log = logging.getLogger("MirrorBot")

		# Create the client; session file will be created like '<session_name>.session'
		self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)

	async def start(self):
		"""Start the Telegram client and register handlers."""
		while True:
			try:
				await self.client.start()
				self.log.info("Client started. Listening for mappings: %s", list(self.raw_mappings.keys()))

				# Resolve any username keys to numeric ids and build final mapping
				await self._resolve_mappings()

				listen_chats = list(self.mappings.keys())
				self.log.info("Resolved mappings: %s", self.mappings)
				if not listen_chats:
					self.log.warning("No source chats resolved — check mappings in config.json. Reconnecting in 5s.")
					await asyncio.sleep(5)
					continue

				@self.client.on(events.NewMessage(chats=listen_chats))
				async def handler(event: events.NewMessage.Event):
					await self._on_message(event)

				# Run until disconnected; Telethon handles reconnections internally,
				# but we wrap start() in an outer loop to recover from unexpected errors.
				await self.client.run_until_disconnected()

			except Exception as exc:
				# handle common errors gracefully and retry
				if isinstance(exc, errors.FloodWaitError):
					wait = exc.seconds if hasattr(exc, "seconds") else 60
					self.log.warning("FloodWait: sleeping %s seconds", wait)
					await asyncio.sleep(wait + 5)
					continue

				self.log.exception("Unexpected error, reconnecting in 5 seconds: %s", exc)
				await asyncio.sleep(5)

	async def _on_message(self, event: events.NewMessage.Event):
		msg = event.message

		# Ignore service messages and empty messages
		if getattr(msg, "action", None) is not None:
			return
		if (not msg.message or msg.message.strip() == "") and not msg.media:
			return

		text = msg.message or ""

		# Keyword filter (if provided)
		if self.keywords:
			lower_text = text.lower()
			if not any(k in lower_text for k in self.keywords):
				self.log.info("Skipping message (no keyword match)")
				return

		# Prepare sending
		try:
			src_id = event.chat_id
			dests = self.mappings.get(src_id, [])
			if not dests:
				self.log.warning("No destination configured for source %s", src_id)
				return

			for dest in dests:
				try:
					if msg.media:
						await self.client.send_file(
							dest,
							msg.media,
							caption=text or None,
							caption_entities=msg.entities if getattr(msg, "entities", None) else None,
						)
					else:
						# For text-only messages, convert Telegram entities to HTML and send with parse_mode
						if getattr(msg, "entities", None):
							try:
								html_text = entities_to_html(text, msg.entities)
								await self.client.send_message(dest, html_text, parse_mode="html")
							except Exception:
								# fallback to plain text if conversion fails
								await self.client.send_message(dest, text)
						else:
							await self.client.send_message(dest, text)

					self.log.info("Mirrored message from %s to %s", src_id, dest)

					if self.delay and self.delay > 0:
						await asyncio.sleep(self.delay)

				except errors.FloodWaitError as fw_inner:
					wait = fw_inner.seconds if hasattr(fw_inner, "seconds") else 60
					self.log.warning("Hit FloodWait when sending to %s: sleeping %s seconds", dest, wait)
					await asyncio.sleep(wait + 5)
				except errors.rpcerrorlist.ChatWriteForbiddenError:
					self.log.error("Permission denied: can't send messages to destination %s", dest)
				except Exception as e_inner:
					self.log.exception("Failed to mirror to %s: %s", dest, e_inner)

		except Exception as e:
			self.log.exception("Failed to mirror message: %s", e)

	async def _resolve_mappings(self):
		"""Resolve mapping keys (usernames) to numeric ids when possible."""
		resolved: Dict[int, List[int]] = {}
		for raw_src, raw_dests in self.raw_mappings.items():
			# normalize raw values
			try:
				src_norm = normalize_id(raw_src)
			except Exception:
				src_norm = raw_src

			self.log.info("Resolving source: %s", src_norm)

			# resolve source to numeric id if needed
			src_id = src_norm
			if isinstance(src_norm, str) and not (src_norm.isdigit() or (src_norm.startswith("-") and src_norm[1:].isdigit())):
				try:
					ent = await asyncio.wait_for(self.client.get_entity(src_norm), timeout=10)
					src_id = ent.id
					self.log.info("Resolved source %s -> %s", src_norm, src_id)
				except asyncio.TimeoutError:
					self.log.warning("Timeout resolving source %s; skipping", src_norm)
					continue
				except Exception as e:
					self.log.warning("Failed to resolve source %s: %s; skipping", src_norm, e)
					continue

			dest_ids: List[int] = []
			for d in raw_dests:
				d_norm = normalize_id(d)
				self.log.info("Resolving destination: %s for source %s", d_norm, src_norm)
				if isinstance(d_norm, str) and not (d_norm.isdigit() or (d_norm.startswith("-") and d_norm[1:].isdigit())):
					try:
						ent = await asyncio.wait_for(self.client.get_entity(d_norm), timeout=10)
						dest_ids.append(ent.id)
						self.log.info("Resolved destination %s -> %s", d_norm, ent.id)
					except asyncio.TimeoutError:
						self.log.warning("Timeout resolving destination %s; skipping", d_norm)
					except Exception as e:
						self.log.warning("Failed to resolve destination %s: %s; skipping", d_norm, e)
				else:
					try:
						dest_ids.append(int(d_norm))
					except Exception:
						self.log.warning("Invalid destination id %s; skipping", d_norm)

			if dest_ids:
				resolved[int(src_id)] = dest_ids
			else:
				self.log.warning("No valid destinations found for source %s; skipping mapping", src_id)

		self.mappings = resolved


def entities_to_html(text: str, entities) -> str:
	"""Convert Telethon MessageEntity list to HTML string for send_message(parse_mode='html').

	This is a best-effort conversion covering common entity types (bold, italic, code,
	pre, text_url, url, mention, text_mention, underline, strike, spoiler).
	"""
	if not entities:
		return _html.escape(text)

	inserts = []
	for ent in entities:
		cls = ent.__class__.__name__
		off = int(ent.offset)
		ln = int(ent.length)
		start_tag = ""
		end_tag = ""

		if cls == "MessageEntityBold":
			start_tag, end_tag = "<b>", "</b>"
		elif cls == "MessageEntityItalic":
			start_tag, end_tag = "<i>", "</i>"
		elif cls == "MessageEntityCode":
			start_tag, end_tag = "<code>", "</code>"
		elif cls == "MessageEntityPre":
			start_tag, end_tag = "<pre>", "</pre>"
		elif cls == "MessageEntityTextUrl":
			url = getattr(ent, "url", "")
			start_tag = f"<a href=\"{_html.escape(url)}\">"
			end_tag = "</a>"
		elif cls == "MessageEntityUrl":
			# URL entity: the text itself is the URL
			start_tag = "<a href=\""
			end_tag = "</a>"
		elif cls in ("MessageEntityMentionName", "MessageEntityTextMention"):
			user_id = getattr(ent, "user_id", None) or getattr(getattr(ent, "user", None), "id", None)
			if user_id:
				start_tag = f"<a href=\"tg://user?id={int(user_id)}\">"
				end_tag = "</a>"
		elif cls == "MessageEntityUnderline":
			start_tag, end_tag = "<u>", "</u>"
		elif cls == "MessageEntityStrike":
			start_tag, end_tag = "<s>", "</s>"
		elif cls == "MessageEntitySpoiler":
			start_tag, end_tag = "<tg-spoiler>", "</tg-spoiler>"
		else:
			# Unsupported entity: skip formatting
			continue

		inserts.append((off, start_tag))
		inserts.append((off + ln, end_tag))

	# Escape text, then apply inserts by positions. Because escaping changes lengths,
	# we apply tags to the unescaped text and finally escape parts without tags.
	# Simpler approach: build final by walking original text and inserting tags.
	inserts_by_pos = {}
	for pos, tag in inserts:
		inserts_by_pos.setdefault(pos, []).append(tag)

	out = []
	for i in range(len(text) + 1):
		if i in inserts_by_pos:
			# opening/closing tags: ensure opening tags appear before escaping
			for tag in inserts_by_pos[i]:
				out.append(tag)
		if i < len(text):
			out.append(_html.escape(text[i]))

	return "".join(out)


def load_config(path: str = "config.json") -> dict:
	if not os.path.exists(path):
		print(f"Config file not found: {path}")
		sys.exit(1)
	with open(path, "r", encoding="utf-8") as f:
		cfg = json.load(f)
	return cfg


def normalize_id(value):
	# Accept ints or strings; if string numeric, convert to int
	if isinstance(value, int):
		return value
	if isinstance(value, str):
		if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
			return int(value)
		# leave as username (str)
		return value
	return value


if __name__ == "__main__":
	cfg = load_config("config.json")

	api_id = int(cfg.get("api_id"))
	api_hash = cfg.get("api_hash")
	session_name = cfg.get("session_name", "mirror_session")

	# Support new 'mappings' configuration or legacy 'source_ids'+'destination_id'
	raw_mappings = {}
	if "mappings" in cfg and isinstance(cfg["mappings"], list):
		for item in cfg["mappings"]:
			src = item.get("source_id") or item.get("source")
			dst = item.get("destination_id") or item.get("destination")
			if src is None or dst is None:
				continue
			# allow destination to be a list or single value
			if isinstance(dst, list):
				raw_mappings[src] = dst
			else:
				raw_mappings[src] = [dst]
	else:
		raw_sources = cfg.get("source_ids")
		destination = cfg.get("destination_id")
		if raw_sources is None or destination is None:
			print("Please configure 'mappings' (preferred) or 'source_ids' + 'destination_id' in config.json")
			sys.exit(1)
		if isinstance(raw_sources, list):
			for s in raw_sources:
				raw_mappings[s] = [destination]
		else:
			raw_mappings[raw_sources] = [destination]

	delay = float(cfg.get("delay_seconds", 2.0))
	enable_logs = bool(cfg.get("enable_logs", True))
	keywords = cfg.get("keywords", [])

	bot = MirrorBot(
		api_id=api_id,
		api_hash=api_hash,
		session_name=session_name,
		raw_mappings=raw_mappings,
		delay=delay,
		enable_logs=enable_logs,
		keywords=keywords,
	)

	try:
		asyncio.run(bot.start())
	except (KeyboardInterrupt, SystemExit):
		print("Exiting...")
