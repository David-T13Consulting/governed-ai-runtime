"""query - ask the vault a question, read-only, from a local model."""
import argparse
import sys

from . import adapter as adapter_mod
from . import answer, config, outpath, scope, trace


def main(argv=None):
    ap = argparse.ArgumentParser(prog="query", description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--profile", default="default", choices=sorted(config.PROFILES))
    ap.add_argument("--endpoint", default=None, help="loopback model endpoint")
    ap.add_argument("--limit", type=int, default=4, help="evidence chunks to retrieve")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--stub-model", action="store_true", help="testing only")
    args = ap.parse_args(argv)

    profile = config.PROFILES[args.profile]
    model = (
        adapter_mod.StubAdapter()
        if args.stub_model
        else adapter_mod.LlamaCppAdapter(endpoint=args.endpoint)
    )

    try:
        result = answer.ask(args.question, profile=profile, model=model, limit=args.limit)
    except adapter_mod.ModelUnavailable as exc:
        # MalformedResponse included. The query read the corpus and called the model, so
        # it is traced before failing (round D finding 3).
        print("MODEL UNAVAILABLE: %s" % exc, file=sys.stderr)
        partial = getattr(exc, "partial", None)
        if partial is not None and not args.no_trace:
            try:
                trace.write(partial, model, profile)
            except outpath.OutsideVaultError as texc:
                print("TRACE REFUSED: %s" % texc, file=sys.stderr)
                return 6
        return 3
    except adapter_mod.NonLocalEndpoint as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 4
    except scope.ScopeError as exc:
        print("SCOPE: %s" % exc, file=sys.stderr)
        return 5

    print(result.render())
    if not args.no_trace:
        try:
            trace.write(result, model, profile)
        except outpath.OutsideVaultError as exc:
            print("TRACE REFUSED: %s" % exc, file=sys.stderr)
            return 6
    return 0 if result.status != answer.WITHHELD else 2


if __name__ == "__main__":
    raise SystemExit(main())
