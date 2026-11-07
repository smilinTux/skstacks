# Run inside Authentik: ak shell < apply_branding.py
# Applies the SKWorld brand to the default brand, makes sure the stock
# password-recovery flow exists, and wires "Forgot password?" into the login
# page. Idempotent. Files are deployed to /media/public/skworld/ by the role
# (see README "Branding"). No secrets here: recovery email uses sksso's global
# email settings.
import os

from authentik.blueprints.v1.importer import Importer
from authentik.brands.models import Brand
from authentik.flows.models import Flow
from authentik.stages.identification.models import IdentificationStage

TITLE = os.environ.get("SKWORLD_BRAND_TITLE", "SKWorld")
FLOW_TITLE = os.environ.get("SKWORLD_FLOW_TITLE", "Welcome to the Kingdom")
PACK = "/media/public/skworld"
RECOVERY_SLUG = "default-recovery-flow"
BLUEPRINT = "/blueprints/example/flows-recovery-email-verification.yaml"

# 1. Recovery flow: apply Authentik's stock blueprint only when the flow is
# missing, so a flow an operator has customised is never overwritten.
recovery = Flow.objects.filter(slug=RECOVERY_SLUG).first()
if recovery:
    print("flow ok (already present)", RECOVERY_SLUG)
else:
    try:
        with open(BLUEPRINT) as fh:
            importer = Importer.from_string(fh.read())
        valid, _logs = importer.validate()
        if not valid:
            print("missing flow (blueprint failed validation)", RECOVERY_SLUG)
        elif importer.apply():
            print("flow applied", RECOVERY_SLUG)
        else:
            print("missing flow (blueprint apply failed)", RECOVERY_SLUG)
    except Exception as exc:  # report, keep branding going
        print("missing flow (%s)" % type(exc).__name__, RECOVERY_SLUG)
    recovery = Flow.objects.filter(slug=RECOVERY_SLUG).first()

# 2. Default brand.
css = open(PACK + "/skworld.css").read()
b = Brand.objects.get(default=True)
b.branding_title = TITLE
b.branding_logo = "skworld/skworld-logo.svg"
b.branding_favicon = "skworld/skworld-favicon.svg"
b.branding_custom_css = css
if recovery:
    b.flow_recovery = recovery
b.save()
print("brand ok", b.branding_title, len(css), "recovery" if recovery else "no-recovery")

# 2b. Login flow heading ("Welcome to authentik!" by default).
if b.flow_authentication and FLOW_TITLE and b.flow_authentication.title != FLOW_TITLE:
    b.flow_authentication.title = FLOW_TITLE
    b.flow_authentication.save()
print("flow title ok", b.flow_authentication.title if b.flow_authentication else "(no login flow)")

# 3. Identification stage(s) of the brand's login flow: show "Forgot password?".
if recovery and b.flow_authentication:
    stages = IdentificationStage.objects.filter(
        flowstagebinding__target=b.flow_authentication
    ).distinct()
    n = 0
    for s in stages:
        if s.recovery_flow_id != recovery.pk:
            s.recovery_flow = recovery
            s.save()
        n += 1
        print("stage ok", s.name)
    if not n:
        print("missing stage (no identification stage on the login flow)")
elif not recovery:
    print("missing stage (no recovery flow to link)")
else:
    print("missing stage (brand has no authentication flow)")
