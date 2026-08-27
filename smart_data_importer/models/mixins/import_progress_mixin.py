from odoo import models, fields, api, _

class ImportProgressMixin(models.AbstractModel):
    _name = 'import.progress.mixin'
    _description = 'Import Execution & Progress Tracking Mixin'

    start_time = fields.Datetime(string='Start Time', readonly=True)
    end_time = fields.Datetime(string='End Time', readonly=True)
    
    processed_rows = fields.Integer(string='Processed Rows', readonly=True, default=0)
    updated_rows = fields.Integer(string='Updated Rows', readonly=True, default=0)
    skipped_rows = fields.Integer(string='Skipped Rows', readonly=True, default=0)
    failed_rows = fields.Integer(string='Failed Rows', readonly=True, default=0)

    progress_percent = fields.Float(string='Progress (%)', compute='_compute_progress_metrics')
    duration_seconds = fields.Float(string='Duration (s)', compute='_compute_progress_metrics')
    speed_rows_per_sec = fields.Float(string='Speed (rows/s)', compute='_compute_progress_metrics')

    @api.depends('processed_rows', 'total_rows', 'start_time', 'end_time')
    def _compute_progress_metrics(self):
        for record in self:
            total = getattr(record, 'total_rows', 0) or 0
            processed = record.processed_rows or 0
            
            # Progress %
            record.progress_percent = round((processed / total * 100), 1) if total > 0 else 0.0
            
            # Duration & Speed
            if record.start_time:
                end = record.end_time or fields.Datetime.now()
                duration = (end - record.start_time).total_seconds()
                record.duration_seconds = round(duration, 2)
                record.speed_rows_per_sec = round(processed / duration, 1) if duration > 0 else 0.0
            else:
                record.duration_seconds = 0.0
                record.speed_rows_per_sec = 0.0
