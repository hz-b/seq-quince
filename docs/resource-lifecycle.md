# Device resource lifecycle

The lifecycle API provides await device.close() for repeatable resource cleanup and async with DeviceScope(): for automatic ownership and cleanup of registered devices. Adapters use track_resource() to register owned resources and their cleanup functions, run_blocking() to track executor work, and run_operation() to track asynchronous work through cancellation. retained_devices() lists devices awaiting safe disposal; CleanupError reports cleanup failures when no execution error already takes priority.
