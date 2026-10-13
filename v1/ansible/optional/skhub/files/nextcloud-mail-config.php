// skhub: apply the vault mail settings to Nextcloud's system config.
//
// Run INSIDE the Nextcloud container as the config owner, with the code on
// argv and the settings on STDIN (never on argv: they carry the SMTP
// password):
//   docker exec -i -u www-data <cid> php -r "$SKHUB_MAIL_PHP" < {"system": {...}}
// No opening tag on purpose: `php -r` takes bare code.
//
// Idempotent: compares the wanted keys with the EFFECTIVE config (config.php
// merged with config/*.config.php, the way Nextcloud reads it; the password
// by sha256 only) and writes nothing when they match. Otherwise it writes the
// settings to a 0600 temp file, runs `occ config:import <file>` from an argv
// array (no shell), overwrites and removes the file, and re-checks.
// Prints "UNCHANGED", or "CHANGED: <key names>". Never prints a value.
// NC_ROOT (default /var/www/html) is for tests.

$root = getenv('NC_ROOT') ?: '/var/www/html';
$input = json_decode(stream_get_contents(STDIN), true);
if (!is_array($input) || !isset($input['system']) || !is_array($input['system']) || !$input['system']) {
    fwrite(STDERR, "skhub-mail: stdin must be {\"system\": {...}}\n");
    exit(2);
}
$want = $input['system'];
$secret_keys = ['mail_smtppassword'];

function skhub_mail_effective($root) {
    $merged = [];
    $files = array_merge([$root . '/config/config.php'], glob($root . '/config/*.config.php') ?: []);
    foreach ($files as $file) {
        if (!is_file($file)) {
            continue;
        }
        $CONFIG = [];
        include $file;
        if (is_array($CONFIG)) {
            $merged = array_merge($merged, $CONFIG);
        }
    }
    return $merged;
}

function skhub_mail_differs($want, $have, $secret_keys) {
    $keys = [];
    foreach ($want as $key => $value) {
        if (!array_key_exists($key, $have)) {
            $keys[] = $key;
        } elseif (in_array($key, $secret_keys, true)) {
            if (!is_string($have[$key]) || !hash_equals(hash('sha256', (string) $value), hash('sha256', $have[$key]))) {
                $keys[] = $key;
            }
        } elseif ($have[$key] !== $value) {
            $keys[] = $key;
        }
    }
    return $keys;
}

$diff = skhub_mail_differs($want, skhub_mail_effective($root), $secret_keys);
if (!$diff) {
    echo "UNCHANGED\n";
    exit(0);
}

$old = umask(077);
$tmp = tempnam(sys_get_temp_dir(), 'skhub-mail-');
umask($old);
$rc = 1;
try {
    chmod($tmp, 0600);
    file_put_contents($tmp, json_encode(['system' => $want]));
    $proc = proc_open([PHP_BINARY, $root . '/occ', 'config:import', $tmp],
                      [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (is_resource($proc)) {
        stream_get_contents($pipes[1]);
        stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        $rc = proc_close($proc);
    }
} finally {
    if (is_file($tmp)) {
        $size = filesize($tmp);
        $fh = fopen($tmp, 'r+');
        if ($fh) {
            fwrite($fh, str_repeat("\0", max(1, (int) $size)));
            fflush($fh);
            fclose($fh);
        }
        unlink($tmp);
    }
}
if ($rc !== 0) {
    fwrite(STDERR, "skhub-mail: occ config:import failed (rc $rc)\n");
    exit(1);
}
$left = skhub_mail_differs($want, skhub_mail_effective($root), $secret_keys);
if ($left) {
    fwrite(STDERR, "skhub-mail: still differs after import: " . implode(',', $left) . "\n");
    exit(1);
}
echo "CHANGED: " . implode(',', $diff) . "\n";
