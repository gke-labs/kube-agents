# kube-agents success criteria

## High level summary

This document lists the success criteria (SC) for kube-agents from the end user's point of view. A success criterion is a result a user can observe and measure. The list answers three questions. Does a user who gets every criterion think kube-agents is good? Which criteria have a gap today? How do we close each gap? What gets measured gets built.

## Goal

In an agentic development project, what gets measured gets built. This document tracks the important success criteria from the end user's point of view, so that agents, engineers and other product stakeholders agree on three questions:

1. If we achieve every SC, will the user think kube-agents is great?
2. Which gaps to the SC exist today?
3. How do we close each gap?

## List of SC

1. Agentic installation and onboarding.
2. New-user-friendly out-of-box experience (OOBE):
   - it does not crash when the install stays within the documented limits;
   - the user gets a report within the first two hours.
3. Responsive to simple questions, under one minute:
   - "what model are you running", "which GCP project have you authenticated with", and the like.
4. Responsive to complex requests:
   - an initial acknowledgement within 10 seconds;
   - "Audit any potential issues for my next GKE version upgrade", "Audit, and discover any zonal skew or anomaly that may cause issues soon", and the like;
   - not more than three times slower than a similar query asked directly to Gemini or Claude;
   - responsive to "try again" after a failure or a timeout.

## Gaps

Open. The gap audit fills this section.
