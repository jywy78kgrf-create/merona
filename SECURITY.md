# Security & responsible disclosure

## Reporting

Found a real vulnerability — in this repo, or in the live API/site
(`merona.io`, `api.merona.io`)? Email **hello@merona.io**. Include a repro:
what you did, what you expected, what actually happened. We acknowledge
within 48 hours.

## What happens next

We verify the report ourselves, fix confirmed issues, and credit the
reporter publicly — commit message and code comment — typically same-day
for confirmed findings. That's the whole process. No ticket queue, no
back-and-forth over severity scoring.

## No bounty program

merona does not pay for bug reports. We fix our own bugs and credit the
people who find them. Our code and data are public precisely so anyone can
check them.

## Scope

In scope: this repository, and the public endpoints it serves
(`merona.io`, `api.merona.io`).

Out of bounds:
- DoS / volumetric testing against either endpoint
- spending attacks against the paid (x402) lane beyond a good-faith minimal
  repro — one payment to demonstrate the bug, not a drain
- social engineering of anyone associated with the project

## Safe harbor

Good-faith security research within the above scope will not be pursued,
regardless of whether you found something or not.
