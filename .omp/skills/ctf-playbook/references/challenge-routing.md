# Challenge Routing

Use this file to decide which reference set to load first. Start from artifacts,
then confirm with keywords and service behavior.

## Artifact-First Triage

| Signal | Route to |
| --- | --- |
| model weights, `.pt`, `.pth`, `.onnx`, `.h5`, prompt-injection tasks, adversarial examples, ML service APIs | `references/categories/ai-ml/` |
| `.pcap`, `.pcapng`, `.evtx`, `.raw`, `.dd`, `.E01`, memory dumps, suspicious media files | `references/categories/forensics/` |
| ELF, EXE, DLL, shared objects, no-extension binaries, remote crashy services | `references/categories/pwn/` or `references/categories/reverse/` |
| `.py`, `.sage`, big integers, ciphertext blobs, modular equations | `references/categories/crypto/` |
| `.apk`, `.wasm`, `.pyc`, mobile apps, decompiler output, custom bytecode | `references/categories/reverse/` |
| URL, HTTP service, HTML/JS/PHP/templates, browser workflow | `references/categories/web/` |
| `.php`, `composer.json`, Laravel, Symfony, WordPress, ThinkPHP, CodeIgniter, Yii | `references/web-source/php-guide.md` |
| `pom.xml`, `build.gradle`, Spring MVC, Servlet, JAX-RS, `WEB-INF`, `.jar`, `.class` | `references/web-source/java-guide.md` |
| images, audio, PDFs, office docs with hidden content hints | `references/categories/forensics/` |
| usernames, social profiles, maps, landmarks, DNS, public records | `references/categories/osint/` |
| packed samples, suspicious scripts, beaconing traffic, PE/.NET implants | `references/categories/malware/` |
| jails, weird encodings, RF/SDR, games, VM puzzles, constrained execution | `references/categories/misc/` |

## Keyword Hints

- Pwn: `overflow`, `ROP`, `shellcode`, `heap`, `libc`, `format string`,
  `seccomp`, `sandbox`
- Crypto: `RSA`, `AES`, `ECC`, `PRNG`, `modulus`, `nonce`, `oracle`,
  `lattice`, `LWE`
- AI/ML: `adversarial`, `model extraction`, `membership inference`,
  `prompt injection`, `jailbreak`, `LoRA`, `gradient`
- Web: `XSS`, `SQLi`, `SSRF`, `SSTI`, `JWT`, `upload`, `deserialization`,
  `prototype pollution`
- Forensics: `disk image`, `memory dump`, `packet capture`, `registry`,
  `stego`, `spectrogram`, `side-channel`
- Reverse: `obfuscated`, `bytecode`, `VM`, `decompile`, `anti-debug`,
  `validation routine`
- OSINT: `find`, `identify`, `who`, `where`, `geolocate`, `username`
- Malware: `beacon`, `loader`, `rat`, `config extraction`, `packed`,
  `persistence`
- Misc: `jail`, `encoding`, `escape`, `game`, `RF`, `SDR`, `constraint`

## Service-Behavior Hints

- Interactive TCP service that crashes on long input: start with pwn.
- HTTP service with forms, headers, cookies, or browser interaction: start with
  web.
- Netcat math or number puzzles: start with crypto.
- ML inference endpoint, suspicious prompt guard, or downloadable model: start
  with AI/ML.
- Restricted shell or eval-like interaction: start with misc.
- Binary that must be understood before exploitation: start with reverse, then
  pivot to pwn if needed.

## Hybrid Patterns

- Web + Crypto: JWT forgery, MAC/signature bugs, custom auth tokens.
- Reverse + Pwn: reverse the binary to recover offsets, logic, or gadgets first.
- Forensics + Crypto: recover ciphertext or keys from captures or disk artifacts.
- Web + Reverse: WASM, obfuscated JS, mobile backends, protocol reconstruction.
- Web + AI/ML: chatbot prompt extraction, model-serving auth bugs, jailbreaks.
- Forensics + OSINT: extract identifiers, then trace them through public data.
- Misc + Crypto: jail or constrained environment used to implement or break a
  crypto primitive.

## Flag Validation

- Prefer the candidate tied to the intended artifact, endpoint, or exploit
  path, not random string noise.
- Check the challenge's expected wrapper such as `flag{...}`, `CTF{...}`,
  `HTB{...}`, or other custom prefixes.
- Record the file path, response, or memory region where the candidate came
  from before finalizing it.
