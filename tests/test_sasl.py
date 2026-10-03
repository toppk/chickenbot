"""Identity is a knob, never a requirement.

chickenbot will log in with a certificate, with a password, with both as
fallbacks for each other, or not at all. Nothing here may make being
identified a condition of joining a channel.
"""

import asyncio
import base64

from chickenbot.irc import Client, parse


def client(**kw) -> Client:
    c = Client(host="x", port=6697, nick="chickenbot", send_interval=0.0, **kw)
    c._outbox = asyncio.Queue(maxsize=100)
    return c


def sent(c: Client) -> list[str]:
    out = []
    while not c._outbox.empty():
        out.append(c._outbox.get_nowait())
    return out


async def offer(c: Client, mechs: str = "PLAIN") -> None:
    """The server's CAP LS, then its ACK of what we asked for."""
    await c._handle_protocol(parse(f":toy CAP * LS :sasl={mechs} message-tags account-tag"))
    asked = [line for line in sent(c) if line.startswith("CAP REQ")]
    if asked:
        granted = asked[0].split(":", 1)[1]
        await c._handle_protocol(parse(f":toy CAP * ACK :{granted}"))


# -- what gets chosen ----------------------------------------------------


async def test_no_credentials_means_no_sasl_at_all():
    """It joins as a nobody rather than refusing to join."""
    c = client()
    await offer(c)
    lines = sent(c)
    assert not any(line.startswith("AUTHENTICATE") for line in lines)
    assert any(line.startswith("CAP END") for line in lines)


async def test_a_password_alone_is_plain():
    c = client(sasl_user="chickenbot", sasl_password="hunter2")
    await offer(c)
    assert "AUTHENTICATE PLAIN" in sent(c)


async def test_a_certificate_is_external_when_the_server_offers_it():
    c = client(tls_cert="/tmp/does-not-need-to-exist.pem")
    await offer(c, "EXTERNAL,PLAIN")
    assert "AUTHENTICATE EXTERNAL" in sent(c)


async def test_external_sends_no_credential():
    """The handshake was the credential; there is nothing left to send."""
    c = client(tls_cert="/tmp/x.pem")
    await offer(c, "EXTERNAL,PLAIN")
    sent(c)
    await c._handle_protocol(parse("AUTHENTICATE +"))
    assert sent(c) == ["AUTHENTICATE +"]


async def test_a_certificate_against_an_older_server_still_uses_the_password():
    """chonkline before the restart lists PLAIN only. Holding a certificate
    must cost nothing until the day the server takes one."""
    c = client(sasl_user="chickenbot", sasl_password="hunter2", tls_cert="/tmp/x.pem")
    await offer(c, "PLAIN")
    assert "AUTHENTICATE PLAIN" in sent(c)


async def test_both_prefers_the_certificate():
    c = client(sasl_user="chickenbot", sasl_password="hunter2", tls_cert="/tmp/x.pem")
    await offer(c, "EXTERNAL,PLAIN")
    assert "AUTHENTICATE EXTERNAL" in sent(c)


# -- old and new, always -------------------------------------------------


async def test_a_refused_certificate_falls_back_to_the_password():
    """chonkline allows PLAIN after a failed EXTERNAL, before CAP END."""
    c = client(sasl_user="chickenbot", sasl_password="hunter2", tls_cert="/tmp/x.pem")
    await offer(c, "EXTERNAL,PLAIN")
    sent(c)
    await c._handle_protocol(parse(":toy 904 * :SASL authentication failed"))
    assert "AUTHENTICATE PLAIN" in sent(c)
    await c._handle_protocol(parse("AUTHENTICATE +"))
    assert base64.b64decode(sent(c)[0].split(" ", 1)[1]) == b"chickenbot\0chickenbot\0hunter2"


async def test_a_refused_certificate_with_no_password_joins_anyway():
    """Being unidentified is a worse seat, not a locked door."""
    c = client(tls_cert="/tmp/x.pem")
    await offer(c, "EXTERNAL,PLAIN")
    sent(c)
    await c._handle_protocol(parse(":toy 904 * :SASL authentication failed"))
    assert any(line.startswith("CAP END") for line in sent(c))


async def test_nothing_is_tried_twice():
    c = client(sasl_user="chickenbot", sasl_password="hunter2", tls_cert="/tmp/x.pem")
    await offer(c, "EXTERNAL,PLAIN")
    for _ in range(3):
        await c._handle_protocol(parse(":toy 904 * :no"))
    lines = sent(c)
    assert lines.count("AUTHENTICATE EXTERNAL") == 1
    assert lines.count("AUTHENTICATE PLAIN") == 1
    assert any(line.startswith("CAP END") for line in lines)


async def test_a_reconnect_may_try_everything_again():
    c = client(sasl_user="chickenbot", sasl_password="hunter2", tls_cert="/tmp/x.pem")
    await offer(c, "EXTERNAL,PLAIN")
    await c._handle_protocol(parse(":toy 904 * :no"))
    sent(c)
    c._sasl_mechs.clear()
    c._sasl_tried.clear()  # what _connect does on the way back up
    await offer(c, "EXTERNAL,PLAIN")
    assert "AUTHENTICATE EXTERNAL" in sent(c)


# -- configuration -------------------------------------------------------


def test_the_certificate_path_is_relative_to_the_toml(tmp_path):
    from chickenbot.config import load

    (tmp_path / "chickenbot.pem").write_text("not read at load time")
    toml = tmp_path / "c.toml"
    toml.write_text('[irc]\nenabled = true\nhost = "x"\nowners = ["toppk"]\ntls_cert = "chickenbot.pem"\n')
    assert load(str(toml)).irc.tls_cert == str(tmp_path / "chickenbot.pem")


def test_no_certificate_configured_is_the_default():
    from chickenbot.config import IRCConfig

    assert IRCConfig().tls_cert == ""


# -- the certificate itself ----------------------------------------------


def cert_cli(tmp_path, *args):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{tmp_path / "c.db"}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["-c", str(toml), "cert", *args])
    return code, out.getvalue() + err.getvalue()


def test_a_generated_certificate_is_what_the_client_will_load(tmp_path):
    """Both halves in one PEM, which is what `load_cert_chain` wants and what
    the server's own walkthrough assumes."""
    import ssl

    from chickenbot.cert import NAME

    code, out = cert_cli(tmp_path)
    pem = (tmp_path / NAME).read_text()
    assert code == 0
    assert pem.count("-----BEGIN") == 2
    ssl.create_default_context().load_cert_chain(tmp_path / NAME)


def test_the_private_key_is_not_world_readable(tmp_path):
    from chickenbot.cert import NAME

    cert_cli(tmp_path)
    assert (tmp_path / NAME).stat().st_mode & 0o077 == 0


def test_the_fingerprint_printed_is_the_one_the_server_will_bind(tmp_path):
    """Lowercase hex SHA-256 of the leaf DER, so it can be diffed against
    what `CERT LIST` prints."""
    from chickenbot.cert import NAME, fingerprint

    _code, out = cert_cli(tmp_path)
    printed = [line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("fingerprint: ")][0]
    assert len(printed) == 64 and printed == printed.lower()
    assert printed == fingerprint((tmp_path / NAME).read_text())


def test_it_will_not_quietly_replace_a_key(tmp_path):
    """Overwriting the key locks the bot out of whatever it was enrolled on."""
    assert cert_cli(tmp_path)[0] == 0
    code, out = cert_cli(tmp_path)
    assert code == 1 and "--force" in out
    assert cert_cli(tmp_path, "--force")[0] == 0


def test_it_lands_beside_the_config_it_belongs_to(tmp_path):
    """Per instance, under the same directory and permissions as the .env."""
    from chickenbot.cert import NAME

    cert_cli(tmp_path)
    assert (tmp_path / NAME).exists()


def test_generating_one_does_not_switch_anything_on(tmp_path):
    """Enrollment is a step on the server. A toml pointing at a certificate
    the account has never seen would fail over to PLAIN without saying so."""
    cert_cli(tmp_path)
    assert "tls_cert" not in (tmp_path / "c.toml").read_text()
