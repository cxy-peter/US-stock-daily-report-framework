# Political Communication Research Layer

## Objective

This layer answers a narrower question than generic sentiment analysis:

> What economically material policy claim did an authoritative person actually
> communicate, how likely is it to progress into implementation, which assets or
> industries are exposed, and how did independent media interpret the claim?

It does **not** count how often an actor says a word. Repetition is an attention
feature, not a policy signal.

## Sources

Direct sources are evaluated before secondary interpretation:

1. signed or implemented presidential actions;
2. White House fact sheets, releases, remarks and press briefings;
3. official speeches and interviews;
4. official X or other authorized social posts;
5. agency statements;
6. company official statements;
7. media direct quotations;
8. media analysis and commentary.

Collectors use public or authorized interfaces only. They do not bypass
logins, paywalls, browser challenges, robots controls or anti-bot measures.
Every failed source remains visible in source health.

## Claim extraction

A communication becomes a candidate claim only when a complete sentence
contains at least one portfolio-relevant policy topic and enough context to
identify an action, view or condition. The model scores:

- actor policy authority;
- source directness;
- implementation stage;
- specificity: date, quantity, responsible authority and action verb;
- novelty versus previously recorded claims;
- recency and stated horizon;
- relevance to current holdings and industry tags;
- media consensus, disagreement and uncertainty.

Example:

```text
Low information:
"AI AI AI. America will win."

Higher information:
"Beginning September 1, Commerce will review a 20 percent tariff on
advanced-semiconductor imports and negotiate exemptions for companies that
expand U.S. production."
```

The second sentence identifies an agency, date, quantity, instrument,
conditional exemption and affected supply chain. It receives a higher
specificity score even if it uses the word "AI" fewer times.

## Actor hierarchy

`examples/political_actor_registry.example.yaml` contains a refreshable,
role-based registry. Current-holder names are metadata with an `as_of` date and
must be revalidated from official sources. The model gives the President the
largest prior because the office can directly create or direct policy. The Vice
President, Treasury, Commerce, USTR, OMB, Energy, the Federal Reserve and major
regulators receive lower topic-specific authority. A spokesperson can clarify
policy but normally receives less authority than the official who can execute
it.

Industry executives are separate from government actors. Their statements are
useful for demand, capacity, supply-chain and regulation transmission, but they
do not constitute government policy.

## Media layer

Media does not overwrite the original statement. Independent outlets are used
to estimate:

- whether analysts read the statement as supportive or restrictive;
- implementation uncertainty;
- disagreement across interpretations;
- whether the source omitted an important legal, fiscal or operational
  constraint.

High disagreement reduces confidence and raises the research-review priority.
It is not converted to a directional trade by itself.

## Portfolio boundary

The aggregate political-communication contribution is capped at 8% of the
model decision score. Positive communications can expand the risk multiplier by
at most 2%; adverse communications can tighten it by at most 10%. A claim can:

- increase research priority;
- identify an affected risk group;
- block chasing when uncertainty and crowding are high;
- request thesis or valuation review;
- modestly tighten a risk budget when confirmed by independent market data.

It cannot independently create `OPEN`, `ADD`, `TRIM` or `EXIT`, and it cannot
place an order.

## Academic motivation

The implementation follows evidence that policy-relevant political text is more
informative than raw post volume. Research on Trump communications finds that
macro/trade content, monetary-policy messages, urgency and linguistic
uncertainty can affect foreign exchange, volatility and policy expectations,
while many posts are reactive or economically irrelevant. The practical lesson
is to identify the subset containing policy information, preserve the event
time, and separate market-moving authority from engagement.

References and their exact model implications are maintained in
`docs/ACADEMIC_EVIDENCE_MAP.md`.
