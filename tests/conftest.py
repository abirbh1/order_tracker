def pytest_sessionfinish(session):
    # Shut telemetry down while pytest's captured stdout is still open, instead of at interpreter exit.
    from app import main

    for provider in main.telemetry_providers:
        provider.shutdown()
