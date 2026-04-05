/**
 * Data loading module — loads point index and monthly CSV files via PapaParse.
 *
 * Optimized for memory efficiency:
 * - LRU cache (max 3 entries) prevents unbounded memory growth
 * - Float32Array storage instead of nested objects (~10× less memory)
 * - Stale request cancellation prevents wasted downloads
 * - Reusable value buffer avoids per-frame allocations
 *
 * CSV format: rows indexed by point_id, columns are datetime strings "YYYY-MM-DD HH:MM".
 * Points index: point_id, latitude, longitude, lat_idx, lon_idx.
 */

const DataLoader = {
    basePath: "../data/output",
    pointIndex: null,          // Array of { point_id, latitude, longitude }
    pointIdToIndex: null,      // Map: point_id → array index (for fast lookup)
    nPoints: 0,

    // LRU cache: max 3 entries to prevent memory exhaustion
    // Each entry is ~80MB (28K points × 744 hours × 4 bytes as Float32Array)
    // vs ~500MB+ with nested JS objects
    cache: new Map(),
    MAX_CACHE: 3,

    // Stale request tracking
    _loadGeneration: 0,

    // Reusable value buffer (avoids allocating 28K-entry objects per frame)
    _valueBuffer: null,

    setBasePath(path) {
        this.basePath = path.replace(/\/+$/, "");
        this.cache.clear();
        this._loadGeneration++;
    },

    loadPointIndex() {
        return new Promise((resolve, reject) => {
            const url = `${this.basePath}/points_index.csv`;
            Papa.parse(url, {
                download: true,
                header: true,
                dynamicTyping: true,
                skipEmptyLines: true,
                complete: (results) => {
                    if (results.errors.length > 0) {
                        console.warn("Point index parse warnings:", results.errors);
                    }
                    this.pointIndex = results.data.map(row => ({
                        point_id: row.point_id,
                        latitude: row.latitude,
                        longitude: row.longitude,
                    }));

                    // Build fast point_id → index lookup
                    this.pointIdToIndex = new Map();
                    for (let i = 0; i < this.pointIndex.length; i++) {
                        this.pointIdToIndex.set(this.pointIndex[i].point_id, i);
                    }
                    this.nPoints = this.pointIndex.length;

                    // Pre-allocate reusable value buffer
                    this._valueBuffer = new Float32Array(this.nPoints);

                    console.log(`Loaded ${this.nPoints} points`);
                    resolve(this.pointIndex);
                },
                error: (err) => {
                    reject(new Error(`Failed to load points index: ${err.message}`));
                },
            });
        });
    },

    /**
     * Load monthly data — tries binary .bin first (instant), falls back to CSV.
     *
     * @returns {Promise<Object>} {
     *   timestamps: string[],
     *   data: Float32Array[],  // data[hourIndex] = Float32Array of nPoints values
     *   pointIdToIndex: Map,   // shared reference
     * }
     */
    async loadMonthlyData(year, month, layerPath) {
        const cacheKey = `${year}/${String(month).padStart(2, "0")}/${layerPath}`;
        if (this.cache.has(cacheKey)) {
            const entry = this.cache.get(cacheKey);
            this.cache.delete(cacheKey);
            this.cache.set(cacheKey, entry);
            return entry;
        }

        const generation = ++this._loadGeneration;
        const monthStr = String(month).padStart(2, "0");
        const parts = layerPath.split("/");
        const variable = parts.length > 1 ? parts[1] : parts[0];

        // Try binary first
        const binUrl = `${this.basePath}/${year}/${monthStr}/web/${variable}.bin`;
        try {
            const resp = await fetch(binUrl);
            if (resp.ok) {
                if (generation !== this._loadGeneration) throw new Error("Request superseded");
                const buf = await resp.arrayBuffer();
                const parsed = this._parseBinary(buf);
                this._cacheSet(cacheKey, parsed);
                console.log(`Loaded ${binUrl} (binary, ${(buf.byteLength / 1048576).toFixed(1)} MB)`);
                return parsed;
            }
        } catch (e) {
            if (e.message === "Request superseded") throw e;
            // Binary not available, fall back to CSV
        }

        // Fall back to CSV
        return this.loadMonthlyCSV(year, month, layerPath);
    },

    /**
     * Parse a binary .bin file into the standard data format.
     * Format: uint32 n_points, uint32 n_hours, uint32 ts_block_len,
     *         ts_block (UTF-8 newline-separated timestamps),
     *         n_hours × n_points float32 values.
     */
    _parseBinary(buffer) {
        const view = new DataView(buffer);
        let offset = 0;

        const nPoints = view.getUint32(offset, true); offset += 4;
        const nHours = view.getUint32(offset, true); offset += 4;
        const tsBlockLen = view.getUint32(offset, true); offset += 4;

        // Decode timestamp strings
        const tsBytes = new Uint8Array(buffer, offset, tsBlockLen);
        const tsText = new TextDecoder().decode(tsBytes);
        const timestamps = tsText.split("\n");
        offset += tsBlockLen;

        // Read float32 arrays for each hour
        const data = new Array(nHours);
        for (let h = 0; h < nHours; h++) {
            data[h] = new Float32Array(buffer, offset, nPoints);
            offset += nPoints * 4;
        }

        return { timestamps, data, pointIdToIndex: this.pointIdToIndex };
    },

    _cacheSet(key, value) {
        if (this.cache.size >= this.MAX_CACHE) {
            const oldest = this.cache.keys().next().value;
            this.cache.delete(oldest);
        }
        this.cache.set(key, value);
    },

    /**
     * Load monthly data from CSV (fallback when .bin not available).
     */
    loadMonthlyCSV(year, month, layerPath) {
        const monthStr = String(month).padStart(2, "0");
        const parts = layerPath.split("/");
        const subfolder = parts[0];
        const variable = parts[1];
        const filename = `${year}_${monthStr}_${variable}.csv`;
        const cacheKey = `${year}/${monthStr}/${layerPath}`;

        if (this.cache.has(cacheKey)) {
            // Move to front (most recently used)
            const entry = this.cache.get(cacheKey);
            this.cache.delete(cacheKey);
            this.cache.set(cacheKey, entry);
            return Promise.resolve(entry);
        }

        const url = `${this.basePath}/${year}/${monthStr}/${subfolder}/${filename}`;
        const generation = ++this._loadGeneration;

        return new Promise((resolve, reject) => {
            Papa.parse(url, {
                download: true,
                header: true,
                dynamicTyping: true,
                skipEmptyLines: true,
                complete: (results) => {
                    // Abort if a newer request has been made
                    if (generation !== this._loadGeneration) {
                        console.log(`Discarding stale load for ${cacheKey}`);
                        reject(new Error("Request superseded"));
                        return;
                    }

                    if (results.data.length === 0) {
                        reject(new Error(`No data in ${url}`));
                        return;
                    }

                    const fields = results.meta.fields;
                    const idField = fields[0];
                    const timestamps = fields.slice(1);
                    const nHours = timestamps.length;

                    // Build compact Float32Array storage: one array per hour
                    // This uses ~80MB for 28K points × 744 hours
                    // vs ~500MB+ with nested JS objects
                    const data = new Array(nHours);
                    for (let h = 0; h < nHours; h++) {
                        data[h] = new Float32Array(this.nPoints);
                        data[h].fill(NaN);
                    }

                    // Fill arrays from parsed rows
                    for (const row of results.data) {
                        const pid = row[idField];
                        if (pid == null) continue;
                        const idx = this.pointIdToIndex.get(pid);
                        if (idx === undefined) continue;

                        for (let h = 0; h < nHours; h++) {
                            const val = row[timestamps[h]];
                            if (val != null) data[h][idx] = val;
                        }
                    }

                    const parsed = {
                        timestamps,
                        data,
                        pointIdToIndex: this.pointIdToIndex,
                    };

                    this._cacheSet(cacheKey, parsed);

                    console.log(`Loaded ${url}: ${this.nPoints} points, ${nHours} hours (${(nHours * this.nPoints * 4 / 1048576).toFixed(0)} MB)`);
                    resolve(parsed);
                },
                error: (err) => {
                    reject(new Error(`No data available for this period`));
                },
            });
        });
    },

    /**
     * Get the Float32Array of all point values for a given hour index.
     * Returns the array directly — no allocation needed.
     *
     * @param {Object} parsedData - from loadMonthlyCSV
     * @param {number} hourIndex - index into timestamps array
     * @returns {Float32Array} values indexed by point array index
     */
    getValuesAtHourIndex(parsedData, hourIndex) {
        if (hourIndex < 0 || hourIndex >= parsedData.data.length) return null;
        return parsedData.data[hourIndex];
    },

    /**
     * Find the index of the closest timestamp to a target string.
     * Returns the index (not the timestamp string) for direct array access.
     */
    findClosestTimestampIndex(timestamps, target) {
        if (!timestamps || timestamps.length === 0) return -1;

        // Fast exact match first
        const exact = timestamps.indexOf(target);
        if (exact !== -1) return exact;

        // Binary-ish search by parsing dates
        let bestIdx = 0;
        let bestDiff = Infinity;
        const targetDate = new Date(target.replace(" ", "T") + ":00");

        for (let i = 0; i < timestamps.length; i++) {
            const d = new Date(timestamps[i].replace(" ", "T") + ":00");
            const diff = Math.abs(d - targetDate);
            if (diff < bestDiff) {
                bestDiff = diff;
                bestIdx = i;
            }
        }
        return bestIdx;
    },

    /**
     * Legacy compatibility: get values as { pointId: value } object.
     * Only used for point click info display (single point, not 28K).
     */
    getValueForPoint(parsedData, hourIndex, pointId) {
        const idx = this.pointIdToIndex.get(pointId);
        if (idx === undefined || hourIndex < 0) return null;
        const val = parsedData.data[hourIndex][idx];
        return isNaN(val) ? null : val;
    },
};
