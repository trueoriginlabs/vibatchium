"""Minimal in-process IMAP4 server for testing patchium's email-code polling.

Implements just enough RFC 3501 to exercise `wait_for_email_code` against
the REAL `imaplib.IMAP4` client (the mock test exercises our parsing only;
this exercises imaplib's actual protocol decoder + our integration).

Supported commands:
  - CAPABILITY
  - LOGIN <user> <pass>             (any creds accepted)
  - SELECT <mailbox>
  - SEARCH UNSEEN [FROM "x"]
  - FETCH <uid> (RFC822)            (literal {SIZE} format)
  - STORE <uid> +FLAGS \\Seen
  - LOGOUT

No SSL — bind to 127.0.0.1 only. For testing only.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import List, Tuple


class MiniIMAPServer:
    """RFC 3501 subset server in its own asyncio thread. Use as a fixture:
        srv = MiniIMAPServer()
        srv.add_message(raw_bytes)
        srv.start()  # binds to 127.0.0.1:srv.actual_port
        try: ...
        finally: srv.stop()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.actual_port: int | None = None
        self.messages: List[Tuple[int, bytes]] = []  # (uid, raw RFC 822)
        self.flagged_read: set[int] = set()
        self.login_attempts: List[Tuple[str, str]] = []
        self._next_uid = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def add_message(self, raw: bytes) -> int:
        uid = self._next_uid
        self._next_uid += 1
        self.messages.append((uid, raw))
        return uid

    def start(self) -> None:
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _serve():
                self._server = await asyncio.start_server(
                    self._handle, self.host, self.port,
                )
                self.actual_port = self._server.sockets[0].getsockname()[1]
                self._ready.set()
                async with self._server:
                    try:
                        await self._server.serve_forever()
                    except asyncio.CancelledError:
                        pass

            try:
                loop.run_until_complete(_serve())
            except Exception:  # noqa: BLE001
                pass
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3):
            raise RuntimeError("MiniIMAPServer failed to come up within 3s")

    def stop(self) -> None:
        if self._server is None or self._loop is None:
            return
        loop = self._loop
        srv = self._server

        def _shutdown():
            srv.close()
            for task in asyncio.all_tasks(loop):
                task.cancel()

        try:
            loop.call_soon_threadsafe(_shutdown)
        except Exception:  # noqa: BLE001
            pass
        # Give the loop a moment to drain
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ─── connection handler ───────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK MiniIMAP ready\r\n")
        await writer.drain()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                # Split: <tag> <COMMAND> [<rest>]
                parts = line.split(b" ", 2)
                tag = parts[0].decode("ascii", errors="replace")
                if len(parts) < 2:
                    writer.write(f"{tag} BAD empty command\r\n".encode())
                    await writer.drain()
                    continue
                cmd = parts[1].decode("ascii", errors="replace").upper()
                rest = parts[2].decode("ascii", errors="replace") if len(parts) >= 3 else ""

                handler = getattr(self, f"_cmd_{cmd.lower()}", None)
                if handler is None:
                    writer.write(f"{tag} BAD unknown command {cmd}\r\n".encode())
                    await writer.drain()
                    continue
                cont = await handler(tag, rest, writer)
                await writer.drain()
                if cont is False:  # LOGOUT signals end-of-session
                    return
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # ─── per-command handlers ─────────────────────────────────────────

    async def _cmd_capability(self, tag, rest, w):
        w.write(b"* CAPABILITY IMAP4rev1\r\n")
        w.write(f"{tag} OK CAPABILITY completed\r\n".encode())

    async def _cmd_login(self, tag, rest, w):
        # Tolerant parser for quoted or bare creds
        m = re.match(r'"?([^"\s]+)"?\s+"?([^"\s]+)"?', rest)
        if m:
            self.login_attempts.append((m.group(1), m.group(2)))
        w.write(f"{tag} OK LOGIN completed\r\n".encode())

    async def _cmd_select(self, tag, rest, w):
        n = len(self.messages)
        w.write(f"* {n} EXISTS\r\n".encode())
        w.write(b"* 0 RECENT\r\n")
        w.write(b"* OK [UIDVALIDITY 1] OK\r\n")
        w.write(f"{tag} OK [READ-WRITE] SELECT completed\r\n".encode())

    async def _cmd_search(self, tag, rest, w):
        # Recognize UNSEEN + optional FROM "x"
        from_match = re.search(r'FROM\s+"([^"]+)"', rest, re.I)
        from_needle = from_match.group(1).lower() if from_match else None
        matching = []
        for uid, raw in self.messages:
            if uid in self.flagged_read:
                continue
            if from_needle is not None:
                hdr = re.search(rb'^From:\s*(.+?)\r?$', raw, re.I | re.M)
                if not hdr:
                    continue
                hdr_val = hdr.group(1).decode("ascii", errors="ignore").lower()
                if from_needle.replace("*", "") not in hdr_val:
                    continue
            matching.append(uid)
        uids = " ".join(str(u) for u in matching)
        w.write(f"* SEARCH {uids}\r\n".encode())
        w.write(f"{tag} OK SEARCH completed\r\n".encode())

    async def _cmd_fetch(self, tag, rest, w):
        m = re.match(r"(\d+)\s+\(([^)]+)\)", rest)
        if not m:
            w.write(f"{tag} BAD bad FETCH\r\n".encode())
            return
        target_uid = int(m.group(1))
        for uid, raw in self.messages:
            if uid == target_uid:
                size = len(raw)
                w.write(f"* {uid} FETCH (RFC822 {{{size}}}\r\n".encode())
                w.write(raw)
                w.write(b")\r\n")
                break
        w.write(f"{tag} OK FETCH completed\r\n".encode())

    async def _cmd_store(self, tag, rest, w):
        m = re.match(r"(\d+)\s+\+FLAGS\s+\(?\\?(Seen)\)?", rest, re.I)
        if m:
            target_uid = int(m.group(1))
            self.flagged_read.add(target_uid)
            w.write(f"* {target_uid} FETCH (FLAGS (\\Seen))\r\n".encode())
        w.write(f"{tag} OK STORE completed\r\n".encode())

    async def _cmd_logout(self, tag, rest, w):
        w.write(b"* BYE logout\r\n")
        w.write(f"{tag} OK LOGOUT completed\r\n".encode())
        return False  # signal end-of-session
