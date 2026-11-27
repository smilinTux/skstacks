# Run inside Authentik: ak shell < apply_branding.py
# Applies the NAM brand to the default brand. Idempotent. The emails are branded
# by apply_email.py (shared set in branding/_email, values in nam/email.yml).
# Files are deployed to /media/public/nam/ by the role (see README). No secrets.
import os
from authentik.brands.models import Brand

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
