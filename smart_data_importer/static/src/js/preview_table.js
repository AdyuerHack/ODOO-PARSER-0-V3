/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class PreviewTableWidget extends Component {
    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            error: null,
            viewMode: "transformed", // 'transformed' | 'side_by_side' | 'raw'
            summary: {
                total_rows: 0,
                columns: [],
                raw_headers: [],
                raw_rows: [],
                transformed_rows: [],
                metrics: {},
            },
        });

        onWillStart(async () => {
            await this.loadDashboard();
        });
    }

    async loadDashboard() {
        const recordId = this.props.record.resId;
        if (!recordId) {
            this.state.loading = false;
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        try {
            const data = await this.orm.call("import.session", "get_dashboard_summary", [[recordId]]);
            if (data.error) {
                this.state.error = data.error;
            } else {
                this.state.summary = data;
            }
        } catch (e) {
            console.error("Error loading import dashboard summary:", e);
            this.state.error = e.message || "Failed to load preview.";
        } finally {
            this.state.loading = false;
        }
    }

    setViewMode(mode) {
        this.state.viewMode = mode;
    }

    getConfidenceClass(confidence) {
        if (confidence >= 80) return "badge bg-success text-white";
        if (confidence >= 50) return "badge bg-warning text-dark";
        return "badge bg-danger text-white";
    }

    getSourceBadgeClass(source) {
        switch (source) {
            case "hash":
                return "badge bg-primary text-white";
            case "fuzzy":
                return "badge bg-info text-dark";
            case "llm":
                return "badge bg-purple text-white";
            case "manual":
                return "badge bg-secondary text-white";
            default:
                return "badge bg-light text-muted border";
        }
    }
}

PreviewTableWidget.template = "smart_data_importer.PreviewTable";
PreviewTableWidget.props = {
    ...standardFieldProps,
};

registry.category("fields").add("preview_table", {
    component: PreviewTableWidget,
});
