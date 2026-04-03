# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.x     | Yes       |
| < 2.0   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in Unshadow-AI, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: **ali@avild.com**

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Scope

The following are in scope:
- Vulnerabilities in the Unshadow-AI tool itself
- Command injection via crafted extension names or registry values
- Path traversal in file scanning logic
- Credential exposure in upload functionality
- Insecure default configurations

The following are out of scope:
- Vulnerabilities in third-party software detected by the tool
- Social engineering attacks
- Denial of service against the tool itself (it's a local scanner)
