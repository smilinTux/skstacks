<#
.SYNOPSIS
  skaegis: install (or remove) the upstream Wazuh agent on a Windows host and
  enrol it with the skaegis manager. Delivered and run through the skrac
  (MeshCentral) agent the host already has; see the skaegis SOP.md
  "Windows agents" for the meshctrl commands.

.DESCRIPTION
  Idempotent: a host that already runs the pinned version, points at the
  manager and is enrolled is left alone (status printed). Otherwise it
  downloads the pinned MSI from packages.wazuh.com, refuses it unless its
  SHA-256 matches, installs it silently, writes the enrolment password and
  the manager address, and (re)starts the WazuhSvc service.

  The enrolment password never appears on a command line (not this script's,
  not msiexec's, so not in MeshCentral's event log): it is read from
  -EnrolmentPasswordFile, a file uploaded next to the script, written to the
  agent's authd.pass with an ACL of SYSTEM and Administrators only, and the
  uploaded file is deleted.

  -Remove stops the service, uninstalls the agent and deletes its directory.
  The agent's record on the manager is removed separately (SOP.md "Remove an
  agent").

  Windows PowerShell 5.1 compatible. Runs as SYSTEM (MeshCentral's default).
#>
[CmdletBinding()]
param(
    [string]$ManagerAddress = '',
    [string]$EnrolmentPasswordFile = (Join-Path $env:ProgramData 'skaegis\enrolment.pass'),
    [string]$AgentGroup = 'default',
    [string]$AgentName = $env:COMPUTERNAME,
    [int]$AgentPort = 1514,
    [int]$EnrolmentPort = 1515,
    # Keep these two in step with the skaegis manager images (4.14.10) and
    # the Linux AGENT_VERSION default. The hash is of the published MSI.
    [string]$Version = '4.14.10-1',
    [string]$Sha256 = '446bd5434a463f64c498f31240caabbb654091ea461131a31e71821a0261e823',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AgentDir = Join-Path ${env:ProgramFiles(x86)} 'ossec-agent'
if (-not ${env:ProgramFiles(x86)}) { $AgentDir = Join-Path $env:ProgramFiles 'ossec-agent' }
$ConfFile = Join-Path $AgentDir 'ossec.conf'
$KeysFile = Join-Path $AgentDir 'client.keys'
$PassFile = Join-Path $AgentDir 'authd.pass'
$WorkDir = Join-Path $env:ProgramData 'skaegis'

function Write-Status([string]$Message) {
    Write-Output ("{0} skaegis: {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Message)
}

function Get-WazuhInstall {
    $keys = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')
    Get-ItemProperty -Path $keys -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like 'Wazuh Agent*' } |
        Select-Object -First 1
}

function Test-Enrolled {
    (Test-Path $KeysFile) -and ((Get-Item $KeysFile).Length -gt 0)
}

if ($Remove) {
    $svc = Get-Service -Name WazuhSvc -ErrorAction SilentlyContinue
    if ($svc) { Stop-Service -Name WazuhSvc -Force -ErrorAction SilentlyContinue }
    $inst = Get-WazuhInstall
    if ($inst) {
        $code = $inst.PSChildName
        $p = Start-Process -FilePath msiexec.exe -ArgumentList @('/x', $code, '/qn', '/norestart') -Wait -PassThru
        if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "msiexec /x exited $($p.ExitCode)" }
        Write-Status "agent $($inst.DisplayVersion) uninstalled"
    } else {
        Write-Status 'agent not installed'
    }
    if (Test-Path $AgentDir) { Remove-Item -Recurse -Force $AgentDir }
    if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
    Write-Status 'removed. Delete its record on the manager too (SOP.md "Remove an agent").'
    exit 0
}

if ($ManagerAddress -notmatch '^[A-Za-z0-9][A-Za-z0-9.:-]*$') {
    throw 'ManagerAddress is required: a swarm node address the host reaches on 1514/1515, never a public edge.'
}
if ($AgentGroup -notmatch '^[A-Za-z0-9_.,-]+$') { throw 'AgentGroup must be a plain group name.' }

$inst = Get-WazuhInstall
$wantVersion = ($Version -split '-')[0]
$installed = $inst -and ($inst.DisplayVersion -like "$wantVersion*")

if ($installed -and (Test-Enrolled) -and -not (Test-Path $EnrolmentPasswordFile) -and
    (Select-String -Path $ConfFile -SimpleMatch "<address>$ManagerAddress</address>" -Quiet) -and
    ((Get-Service -Name WazuhSvc -ErrorAction SilentlyContinue).Status -eq 'Running')) {
    Write-Status "agent $($inst.DisplayVersion) already installed, enrolled and running against $ManagerAddress; nothing to do"
    exit 0
}

if (-not (Test-Path $EnrolmentPasswordFile)) {
    if (-not (Test-Path $PassFile)) {
        throw "No enrolment password: upload it to $EnrolmentPasswordFile first (SOP.md `"Windows agents`")."
    }
    Write-Status 'reusing the enrolment password already in authd.pass'
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

if (-not $installed) {
    $msi = Join-Path $WorkDir "wazuh-agent-$Version.msi"
    $url = "https://packages.wazuh.com/4.x/windows/wazuh-agent-$Version.msi"
    Write-Status "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
    $hash = (Get-FileHash -Algorithm SHA256 -Path $msi).Hash.ToLowerInvariant()
    if ($hash -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -Force $msi
        throw "SHA-256 mismatch for $url (got $hash): refusing to install."
    }
    # No password on this command line: authd.pass is written below.
    $msiArgs = @('/i', "`"$msi`"", '/qn', '/norestart',
              "WAZUH_MANAGER=$ManagerAddress", "WAZUH_MANAGER_PORT=$AgentPort",
              "WAZUH_REGISTRATION_SERVER=$ManagerAddress", "WAZUH_REGISTRATION_PORT=$EnrolmentPort",
              "WAZUH_AGENT_GROUP=$AgentGroup", "WAZUH_AGENT_NAME=$AgentName")
    $p = Start-Process -FilePath msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
    Remove-Item -Force $msi
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "msiexec /i exited $($p.ExitCode)" }
    Write-Status "agent $Version installed"
}

if (Test-Path $EnrolmentPasswordFile) {
    $pw = (Get-Content -Raw -Path $EnrolmentPasswordFile).Trim()
    if ($pw.Length -lt 16) { throw 'The enrolment password file holds fewer than 16 characters.' }
    Set-Content -Path $PassFile -Value $pw -NoNewline -Encoding ASCII
    $pw = $null
    Remove-Item -Force $EnrolmentPasswordFile
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($who in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($who, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -Path $PassFile -AclObject $acl
    Write-Status 'enrolment password written (SYSTEM and Administrators only), upload deleted'
}

$conf = Get-Content -Raw -Path $ConfFile
$new = [regex]::Replace($conf, '<address>[^<]*</address>', "<address>$ManagerAddress</address>")
if ($new -ne $conf) {
    # No BOM: Windows PowerShell 5.1's -Encoding UTF8 writes one.
    [IO.File]::WriteAllText($ConfFile, $new, (New-Object System.Text.UTF8Encoding($false)))
    Write-Status "manager address set to $ManagerAddress"
}

Set-Service -Name WazuhSvc -StartupType Automatic
Restart-Service -Name WazuhSvc -Force

for ($i = 0; $i -lt 24 -and -not (Test-Enrolled); $i++) { Start-Sleep -Seconds 5 }
if (Test-Enrolled) {
    Write-Status "enrolled with $ManagerAddress as $AgentName (group $AgentGroup); WazuhSvc running"
    exit 0
}
Write-Status "NOT ENROLLED after 2 minutes: check that ${ManagerAddress}:$EnrolmentPort is reachable and the password matches; see $AgentDir\ossec.log"
exit 1
