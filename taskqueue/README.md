# treetime/taskqueue

This module implements multiple-producer-multiple-consumer task queue that
serializes incoming tasks in order to balance the load across workers.

Task producer(s) push tasks into the queue. Task consumers pop tasks from the
queue.

This may be used as is (e.g. locally, in dev mode) or may be replaced with an
AWS Batch or other cloud solutions which include message queues/brokers and or
other means of distributing work.
