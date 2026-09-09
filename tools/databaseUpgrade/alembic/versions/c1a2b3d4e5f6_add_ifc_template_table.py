# Copyright 2026 volte.io UG (haftungsbeschränkt)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add IFC template table and foreign key

Revision ID: c1a2b3d4e5f6
Revises: 851e500507f5
Create Date: 2026-01-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a2b3d4e5f6'
down_revision = '851e500507f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create ifc_template table
    op.create_table('ifc_template',
        sa.Column('ifc_template_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('description', sa.String(length=1024), nullable=True),
        sa.Column('template_content', sa.Text(), nullable=False),
        sa.Column('last_modified', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('ifc_template_id'),
        sa.UniqueConstraint('name')
    )
    
    # Add ifc_template_id foreign key to ims_subscriber table
    op.add_column('ims_subscriber', 
        sa.Column('ifc_template_id', sa.Integer(), nullable=True))
    
    # Create foreign key constraint
    op.create_foreign_key(
        'fk_ims_subscriber_ifc_template',
        'ims_subscriber', 'ifc_template',
        ['ifc_template_id'], ['ifc_template_id']
    )


def downgrade() -> None:
    # Drop foreign key constraint first
    op.drop_constraint('fk_ims_subscriber_ifc_template', 'ims_subscriber', type_='foreignkey')
    
    # Drop the column
    op.drop_column('ims_subscriber', 'ifc_template_id')
    
    # Drop the table
    op.drop_table('ifc_template')
