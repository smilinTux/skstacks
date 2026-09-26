"""Every overlay network default must have its own subnet: two services
claiming the same subnet collide on one swarm. Agents publishing in parallel
each picked "free" ranges and chose the same ones (skgallery vs sksso).
cloud-public-<env> is shared on purpose and may repeat."""
import pathlib
import re
from collections import defaultdict

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
PAIR = re.compile(r"name['\"]?\s*:\s*['\"]([\w.-]+)['\"]\s*,\s*['\"]?subnet['\"]?\s*:\s*['\"](\d+\.\d+\.\d+\.\d+/\d+)['\"]")


def test_no_two_networks_share_a_subnet():
    owners = defaultdict(set)
    for p in ANSIBLE.rglob("deploy_*.yml"):
        for name, subnet in PAIR.findall(p.read_text()):
            if not name.startswith("cloud-public"):
                owners[subnet].add(name)
    clashes = {s: sorted(n) for s, n in owners.items() if len(n) > 1}
    assert not clashes, clashes
