# Run inside Authentik: ak shell < apply_branding.py
# Applies the NAM brand to the default brand and points the two email stages at
# the NAM templates. Idempotent. Files are deployed to /media/public/nam/ and
# /templates/ by the role (see README). No secrets here.
import os
from authentik.brands.models import Brand
from authentik.stages.email.models import EmailStage

TITLE = os.environ.get("NAM_BRAND_TITLE", "NAM - Native Asset Management")
css = open("/media/public/nam/nam.css").read()
b = Brand.objects.get(default=True)
b.branding_title = TITLE
b.branding_logo = "nam/nam-logo.svg"
b.branding_favicon = "nam/nam-mark.svg"
b.branding_default_flow_background = "nam/nam-background.jpg"
b.branding_custom_css = css
b.ui_theme = "dark"
b.save()
print("brand ok", b.branding_title, len(css))

STAGES = {
    "skworld-invite-email": ("nam_invite.html", "Welcome to NAM - set up your account"),
    "default-recovery-email": ("nam_recovery.html", "NAM - reset your password"),
}
for name, (tpl, subject) in STAGES.items():
    s = EmailStage.objects.filter(name=name).first()
    if not s:
        print("missing stage", name)
        continue
    s.template = tpl
    s.subject = subject
    s.save()
    print("stage ok", name, tpl)

# Flow titles that still say "authentik" (cosmetic only, no flow logic touched).
from authentik.flows.models import Flow
for f in Flow.objects.filter(title__contains="authentik"):
    new = f.title.replace("authentik", "NAM")
    f.title = new
    f.save()
    print("flow title", f.slug, "->", new)
