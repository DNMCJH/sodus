# rulekit

A small decision engine used by the fraud desk: a request comes in as a plain
mapping (`amount`, `country`, ...), a set of rules is asked "do you apply?",
and the highest-priority matching rule produces the decision payload.

The desk needs the *interface* to be stable before the rules are written: the
review console imports the types directly, and a second team is already
writing rules against them. That is why this task is staged — define the
contract, then make it behave.

Nothing exists yet. Create the package.
