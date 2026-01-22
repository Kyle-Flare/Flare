import cv2

# Optional: only needed to list device names for the "video=<name>" method
try:
    from pygrabber.dshow_graph import FilterGraph
except Exception:
    FilterGraph = None


def list_dshow_devices():
    if FilterGraph is None:
        return []
    try:
        graph = FilterGraph()
        return graph.get_input_devices()
    except Exception:
        return []


def try_open_by_name(device_names):
    """
    Open a DirectShow camera by device name using: VideoCapture("video=<name>", CAP_DSHOW)
    """
    for name in device_names:
        cap = cv2.VideoCapture(f"video={name}", cv2.CAP_DSHOW)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                print(f"Using camera: {name}")
                return cap
        cap.release()
    return None


def try_open_by_index(backends, max_index=6):
    """
    Try opening by index across multiple backends.
    """
    for backend in backends:
        for i in range(max_index):
            cap = cv2.VideoCapture(i, backend)
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    backend_name = {
                        cv2.CAP_DSHOW: "DSHOW",
                        cv2.CAP_MSMF: "MSMF",
                        cv2.CAP_ANY: "ANY",
                    }.get(backend, str(backend))
                    print(f"Using camera index {i} with backend {backend_name}")
                    return cap
            cap.release()
    return None


def open_camera():
    # First choice: open by NAME via DirectShow (best when index capture fails)
    names = list_dshow_devices()
    if names:
        cap = try_open_by_name(names)
        if cap is not None:
            return cap
    else:
        print("No DirectShow device list available (pygrabber not installed or blocked).")

    # Fallbacks: try by index using different backends
    cap = try_open_by_index([cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY], max_index=6)
    if cap is not None:
        return cap

    raise RuntimeError(
        "Could not open any camera.\n"
        "Fixes to try:\n"
        "1) Close apps that may be using the camera (Teams, Zoom, Camera app, browser tabs).\n"
        "2) Windows Settings > Privacy & security > Camera:\n"
        "   - Camera access ON\n"
        "   - Let desktop apps access your camera ON\n"
        "3) Unplug/replug the USB camera, try a different USB port.\n"
    )


def main():
    cap = open_camera()

    # Optional: make preview snappier
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    detector = cv2.QRCodeDetector()

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            print("Failed to read frame from camera.")
            break

        data, points, _ = detector.detectAndDecode(frame)

        # Draw outline if found
        if points is not None and len(points) > 0:
            pts = points.astype(int).reshape(-1, 2)
            for i in range(len(pts)):
                cv2.line(frame, tuple(pts[i]), tuple(pts[(i + 1) % len(pts)]), (0, 255, 0), 2)

        if data:
            print("QR:", data)
            # Uncomment to stop after first scan:
            # break

        cv2.imshow("QR Scanner (press q to quit)", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
