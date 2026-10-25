"""Equal, single-choice tally; abstentions never shrink the majority denominator."""


def tally(spec, ballots):
    counts = {option['id']: 0 for option in spec['options']}
    if len(ballots) != len(spec['participants']) or not ballots:
        raise ValueError('complete participant ballots required')
    abstentions = 0
    for ballot in ballots:
        choice = ballot['choice_id']
        if choice is None:
            abstentions += 1
        elif choice in counts:
            counts[choice] += 1
        else:
            raise ValueError('unknown choice')
    top = max(counts.values())
    leaders = [key for key, count in counts.items() if count == top] if top else []
    outcome = ('all_abstain' if top == 0 else 'tie' if len(leaders) > 1 else
               'majority' if top * 2 > len(ballots) else 'plurality_only')
    return {'counts': counts, 'abstentions': abstentions, 'participants': len(ballots),
            'leaders': leaders, 'outcome': outcome}
