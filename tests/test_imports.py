def test_public_exports():
    from funlab.sched import SchedService, SchedTask, SayHelloTask
    assert issubclass(SayHelloTask, SchedTask)
