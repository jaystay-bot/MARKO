"""Niche config for the MARKO Internet Leak Engine (N002).

One static dict. No taxonomy system, no ML, no external lookup. Each
niche entry tells the engine:

  aliases               substring matches that resolve to this niche
                        (so 'pet groomer' resolves to 'dog_groomer'
                        because that's how the data was scraped)
  service_offer         the canonical $-band suggestion for the report
  baseline_leads_per_mo coarse industry prior (for the heuristic
                        lost-lead band; never claimed as fact)
  weight_overrides      per-leak weight bumps when the leak hits this
                        niche harder than the generic baseline
                        (e.g. 'no_booking_path' costs a med spa more
                        than it costs a barber walk-in shop)
  commercial_themes     short list of visual beats the mini-commercial
                        generator weaves into a 15-second concept

Adding a niche = one new entry here. No code changes elsewhere.
"""
from __future__ import annotations

NICHES = {
    "movers": {
        "aliases": ("movers", "mover", "moving", "moving company",
                    "moving services"),
        "service_offer": (
            "$20-$50 per qualified moving lead via BookerMove "
            "(pay-as-you-go, no contract)."
        ),
        "baseline_leads_per_mo": 30,
        "weight_overrides": {
            "no_quote_form":     30,  # bigger weight than baseline 25
            "no_booking_path":   20,
            "no_visible_contact": 20,
        },
        "commercial_themes": (
            "phone vibrating once and getting auto-replied",
            "foreman waving from a fully-loaded box truck",
            "before/after shots of an empty room then a settled home",
            "text overlay: 'reply in 3 minutes'",
            "smiling family in their new doorway",
        ),
    },

    "barbers": {
        "aliases": ("barber", "barbers", "barbershop", "barber shop",
                    "men's grooming"),
        "service_offer": (
            "$10-$25 per qualified booking lead. Most barbershop value "
            "is repeat -- the first lead is the wedge."
        ),
        "baseline_leads_per_mo": 60,  # higher volume, lower ticket
        "weight_overrides": {
            "no_booking_path":   30,  # appointment business; booking is critical
            "no_short_form_content": 15,  # IG/TikTok cuts drive walk-ins
        },
        "commercial_themes": (
            "clipper-line close-up with crisp ASMR sound",
            "before/after rotation on a single chair",
            "barber laughing with a client; 'walk-in welcome' overlay",
            "QR code to book in <10 seconds",
        ),
    },

    "pet_groomers": {
        "aliases": ("pet groomer", "pet groomers", "dog groomer",
                    "dog grooming", "groomer", "grooming"),
        "service_offer": (
            "$15-$40 per qualified grooming booking lead via BookerMove "
            "(local, pay-as-you-go)."
        ),
        "baseline_leads_per_mo": 35,
        "weight_overrides": {
            "no_booking_path":   25,
            "missing_trust_signals": 20,  # owners trust photos + reviews here
            "no_short_form_content": 15,
        },
        "commercial_themes": (
            "dog wagging tail in a fresh bow",
            "before/after fluff transformation",
            "owner saying 'they came home calm' on camera",
            "text overlay: 'book online in 60 seconds'",
        ),
    },

    "nail_salons": {
        "aliases": ("nail salon", "nail salons", "nails", "manicure",
                    "pedicure"),
        "service_offer": (
            "$10-$30 per qualified booking lead. Volume play -- aim "
            "for repeat-rate, not first-touch margin."
        ),
        "baseline_leads_per_mo": 70,
        "weight_overrides": {
            "no_booking_path":   30,
            "no_short_form_content": 20,
            "missing_trust_signals": 15,
        },
        "commercial_themes": (
            "thumb-zoom on a fresh set, soft camera roll",
            "client smiling at her hands in golden hour light",
            "stylist mixing color on camera (process beat)",
            "text overlay: 'tap to book' over the final hand shot",
        ),
    },

    "med_spas": {
        "aliases": ("med spa", "med spas", "medspa", "medical spa",
                    "aesthetics"),
        "service_offer": (
            "$50-$150 per qualified consultation lead via BookerMove. "
            "High ticket -- conversion to consult is the metric."
        ),
        "baseline_leads_per_mo": 25,
        "weight_overrides": {
            "no_booking_path":     35,
            "missing_trust_signals": 30,  # trust is everything here
            "no_visible_contact":   20,
            "outdated_branding":    20,
        },
        "commercial_themes": (
            "calm clinical-room reveal with soft natural light",
            "practitioner credentials on screen ('RN, 12 yrs')",
            "patient testimonial overlay (1-line, real)",
            "'free consult, no pressure' end card",
        ),
    },

    "roofers": {
        "aliases": ("roofer", "roofers", "roofing", "roofing company"),
        "service_offer": (
            "$50-$200 per qualified residential roofing lead. Storm- "
            "season spikes are the asymmetric upside."
        ),
        "baseline_leads_per_mo": 20,
        "weight_overrides": {
            "no_quote_form":      35,
            "no_visible_contact": 25,
            "missing_trust_signals": 20,
            "no_booking_path":    15,  # roofing isn't booked online -- form > calendar
        },
        "commercial_themes": (
            "drone shot of a freshly-replaced roof",
            "homeowner shaking hands with a crew lead",
            "'free inspection in 48 hours' overlay",
            "BBB / GAF / OWENS-CORNING credential plate",
        ),
    },

    "plumbers": {
        "aliases": ("plumber", "plumbers", "plumbing", "plumbing company"),
        "service_offer": (
            "$25-$75 per qualified service-call lead. Emergency calls "
            "are the highest-margin slice -- after-hours capture matters."
        ),
        "baseline_leads_per_mo": 40,
        "weight_overrides": {
            "no_visible_contact": 30,  # plumbing is phone-call-driven
            "no_quote_form":      20,
            "no_booking_path":    15,
            "missing_trust_signals": 15,
        },
        "commercial_themes": (
            "tech in clean uniform stepping out of a branded van",
            "'24/7 emergency calls answered' overlay",
            "before/after of a clean install (no mess left behind)",
            "homeowner relief shot in a working kitchen",
        ),
    },
}


def resolve_niche(value):
    """Resolve a free-text niche string to a canonical key, or None."""
    if not value:
        return None
    lc = value.strip().lower()
    if lc in NICHES:
        return lc
    for key, cfg in NICHES.items():
        if any(alias in lc or lc in alias for alias in cfg["aliases"]):
            return key
    return None


def all_niches():
    return tuple(NICHES.keys())


def niche_or_default(value):
    """Resolve, falling back to a baseline 'generic' shape if unknown."""
    key = resolve_niche(value)
    if key:
        return key, NICHES[key]
    return None, {
        "aliases": (),
        "service_offer": (
            "$20-$50 per qualified lead via BookerMove (pay-as-you-go)."
        ),
        "baseline_leads_per_mo": 25,
        "weight_overrides": {},
        "commercial_themes": (
            "owner on camera, plain backdrop, one true sentence",
            "before/after of the actual service",
            "one specific 'call now' or 'book online' overlay",
            "single shot of a satisfied real customer",
        ),
    }
