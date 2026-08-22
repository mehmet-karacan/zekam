# Sandbox, Path ve Network Policy

## Workspace sınıfları

- source binding: read-only external root
- Zekam core workspace: Zekam geliştirme scope'u
- managed worktree: integrated project mutation
- verification workspace: read-only patch/test veya isolated execute
- ingestion workspace: untrusted archive/repo read-only extraction
- temporary runtime: bounded, cleanup/recovery tracked

## Path kuralları

Public/portable path:
- relative POSIX
- no `..`
- no backslash ambiguity
- no drive/root/UNC
- no NUL/control
- normalized Unicode policy
- exact project/source binding.

Physical path yalnız local locator store'da ve modele gösterilmeden çözülür.

## Symlink

- discovery symlink metadata'yı görür ancak default follow=false.
- Follow policy açık olsa bile canonical target allowed root içinde olmalıdır.
- Worktree write symlink target allowlist dışına çıkamaz.
- Archive symlink/hardlink default deny.

## Archive

- zip-slip/tar traversal
- absolute path
- symlink/hardlink
- decompressed ratio/total/file count/size
- nested archive depth
- special device/FIFO
- filename encoding

kontrolleri.

## Process sandbox

Typed ProcessRequest:
- executable/argv
- logical cwd
- env allowlist
- stdin ref
- timeout/grace/cancel
- output bytes
- CPU/memory/file/process limits
- network policy
- filesystem mounts
- result artifact.

Shell expansion default deny. Build/test executable allowlist project capability profile'dan
reviewed olur.

## Network

Default deny. Allow:
- exact host/port/protocol/operation
- DNS resolution and redirect revalidation
- TLS hostname/certificate policy
- SSRF private/link-local/metadata endpoint restrictions
- response size/time/content type
- outbound audit.

Model provider internal route policy ayrı olabilir; yine endpoint_ref ve authorization ister.

## Git

- discovery no hooks/submodules/LFS.
- worktree source commit/tree bind.
- changed path exact allowlist.
- main source dirty/HEAD/tree işlem sonunda recheck.
- commit local after tests/verifier.
- push default deny and exact expected remote ref/OID.

## Untrusted code

Ingestion sırasında hiçbir executable/module import/build/test/package manager yok. Static
parser dependency risky grammar ise isolated parser process ve timeout kullanılır.
