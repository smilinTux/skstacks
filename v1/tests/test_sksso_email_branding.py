"""sksso shared email set (branding/_email): every Authentik email rendered per
brand from <pack>/email.yml. No network, no Authentik: renders with jinja2, then
with Django when it is installed."""
import importlib.util
import py_compile
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "sksso"
EMAIL = ROLE / "branding" / "_email"
TASKS = ROLE / "tasks" / "branding.yml"
PACKS = ["skworld", "nam"]
# every template Authentik can send (EmailTemplates choices, OTP and test email)
KINDS = ["password_reset", "account_confirmation", "email_otp", "invitation", "event_notification", "setup"]


def _render_mod():
    spec = importlib.util.spec_from_file_location("sksso_email_render", EMAIL / "render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module", params=PACKS)
def rendered(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param)
    _render_mod().render(ROLE / "branding" / request.param / "email.yml", out)
    return request.param, out


def test_every_kind_has_html_and_txt_sources():
    for k in KINDS:
        for ext in ("html", "txt"):
            assert (EMAIL / "email" / ("%s.%s" % (k, ext))).is_file(), (k, ext)
    assert (EMAIL / "email" / "base.html").is_file()


def test_framework_packs_ship_complete_email_yml():
    req = _render_mod().REQUIRED
    for p in PACKS:
        e = yaml.safe_load((ROLE / "branding" / p / "email.yml").read_text())["e"]
        assert all(str(e.get(k, "")).strip() for k in req), p


def test_render_fills_brand_and_leaves_no_jinja(rendered):
    pack, out = rendered
    name = yaml.safe_load((ROLE / "branding" / pack / "email.yml").read_text())["e"]["name"]
    for f in (out / "email").iterdir():
        text = f.read_text()
        assert "[[" not in text and "[%" not in text and "[#" not in text, f
        assert "authentik" not in text.lower(), f
        assert "<img" not in text and "cid:logo" not in text, f
        assert "—" not in text and "–" not in text, f
    assert name in (out / "email" / "base.html").read_text()
    for k in KINDS:
        if k != "setup":
            assert (out / "email" / ("%s.html" % k)).read_text().startswith('{% extends "email/base.html" %}'), k
    py_compile.compile(str(out / "apply_email.py"), cfile=str(out / "x.pyc"), doraise=True)


def test_missing_brand_value_fails(tmp_path):
    bad = tmp_path / "email.yml"
    bad.write_text("e:\n  name: X\n")
    with pytest.raises(SystemExit):
        _render_mod().render(bad, tmp_path / "out")


def test_apply_email_sets_stages_transports_and_subjects(rendered):
    pack, out = rendered
    code = (out / "apply_email.py").read_text()
    for needle in ('"email/password_reset.html"', '"email/invitation.html"', '"email/account_confirmation.html"',
                   '"email/email_otp.html"', '"email/event_notification.html"', "email_subject_prefix",
                   'title__icontains="authentik"', "AuthenticatorEmailStage"):
        assert needle in code, needle
    for prefix in ("stage", "otp stage", "transport", "flow title"):
        assert re.search(r'print\("%s[ "]' % prefix, code), prefix


def _django(out):
    django = pytest.importorskip("django")
    from django.conf import settings
    from django.template import engines
    if not settings.configured:
        settings.configure(USE_I18N=True, USE_TZ=True, INSTALLED_APPS=["django.contrib.humanize"],
                           TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": []}])
        django.setup()
    from django.template.engine import Engine
    return Engine(dirs=[str(out)], libraries={"i18n": "django.templatetags.i18n",
                                              "humanize": "django.contrib.humanize.templatetags.humanize"})


def test_every_email_renders_in_django(rendered):
    pack, out = rendered
    engine = _django(out)
    from django.template import Context
    from django.utils import timezone
    u = SimpleNamespace(name="Ada & Co", username="ada")
    exp = timezone.now() + timedelta(minutes=30)
    ctxs = {
        "password_reset": {"url": "https://sso.example.test/r?t=1", "user": u, "expires": exp},
        "account_confirmation": {"url": "https://sso.example.test/c", "user": u, "expires": exp},
        "email_otp": {"token": "123456", "user": u, "expires": exp},
        "invitation": {"url": "https://sso.example.test/i", "expires": exp, "host": "sso.example.test"},
        "event_notification": {"title": "login_failed", "body": "Bad login", "link": {"target": "https://x.test", "label": "Open"},
                               "key_value": {"user_email": "a@x.test"}, "source": {"from": "default-email-transport"}},
        "setup": {},
    }
    for k, ctx in ctxs.items():
        for ext in ("html", "txt"):
            text = engine.get_template("email/%s.%s" % (k, ext)).render(Context(ctx))
            assert "authentik" not in text.lower(), (k, ext)
            if "url" in ctx:
                assert ctx["url"] in text, (k, ext)
            if "user" in ctx and k != "email_otp":
                assert ("Ada &amp; Co" if ext == "html" else "Ada & Co") in text, (k, ext)
    otp = engine.get_template("email/email_otp.html").render(Context(ctxs["email_otp"]))
    assert "123456" in otp
    invite = engine.get_template("email/invitation.html").render(Context({**ctxs["invitation"], "user": u}))
    assert "ada" in invite and "Set up my account" in invite


def test_branding_yml_renders_the_email_set_only_when_the_pack_has_email_yml():
    tasks = yaml.safe_load(TASKS.read_text())
    stat = next(t for t in tasks if t.get("name") == "Check whether the pack ships email brand values")
    assert stat["stat"]["path"] == "{{ sksso_branding_src }}/email.yml"
    block = next(t for t in tasks if t.get("name") == "Render the shared branded email set")
    assert block["when"] == "sksso_branding_email.stat.exists"
    names = [t["name"] for t in block["block"]]
    assert "Copy the rendered email templates" in names
    copy = next(t for t in block["block"] if t["name"] == "Copy the rendered email templates")
    assert copy["copy"]["dest"].endswith("/custom-templates/email/")
    assert any("file" in t and t["file"]["state"] == "absent" for t in block["always"])


def test_apply_email_helper_is_safe_shell():
    sh = (EMAIL / "apply-email-branding.sh").read_text()
    assert "set -euo pipefail" in sh
    assert "case \"$PACK\" in" in sh
    for p in PACKS:
        assert 'apply-email-branding" %s "$STACK"' % p in (ROLE / "branding" / p / "apply-branding.sh").read_text(), p
